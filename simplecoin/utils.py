import datetime
import time
import yaml
import requests

from urlparse import urljoin
from flask import current_app, session
from cryptokit.base58 import address_version
from sqlalchemy.exc import SQLAlchemyError
from cryptokit.rpc import CoinserverRPC, CoinRPCException
from decimal import Decimal as dec

from . import db, cache, root, redis_conn, currencies, powerpools, exchanges
from .models import (ShareSlice, Block, Payout, UserSettings, make_upper_lower,
                     PayoutAggregate)


class CommandException(Exception):
    pass


class Currency(object):
    requires = ['algo', 'name', 'coinserv', 'address_version', 'trans_confirmations',
                'block_time', 'block_mature_confirms']

    def __init__(self, key, bootstrap):
        for req in self.requires:
            if req not in bootstrap:
                raise ConfigurationException("currency item requires {}"
                                             .format(req))
        self.key = key
        # Default settings
        self.__dict__.update(dict(exchangeable=False))
        self.__dict__.update(bootstrap)
        self.coinserv = CoinserverRPC(
            "http://{0}:{1}@{2}:{3}/"
            .format(bootstrap['coinserv']['username'],
                    bootstrap['coinserv']['password'],
                    bootstrap['coinserv']['address'],
                    bootstrap['coinserv']['port'],
                    pool_kwargs=dict(maxsize=bootstrap.get('maxsize', 10))))

    @property
    @cache.memoize(timeout=600)
    def btc_value(self):
        """ Caches and returns estimated currency value in BTC """
        if self.key == "BTC":
            return dec('1')

        # XXX: Needs better number here!
        err, dat, _ = exchanges.optimal_sell(self.key, dec('1000'), exchanges._get_current_object().exchanges)
        try:
            current_app.logger.info("Got new average price of {} for {}"
                                    .format(dat['avg_price'], self))
            return dat['avg_price']
        except (KeyError, TypeError):
            current_app.logger.warning("Unable to grab price for currency {}, got {} from autoex!"
                                       .format(self.key, dict(err=err, dat=dat)))
            return dec('0')

    def est_value(self, other_currency, amount):
        val = self.btc_value
        if val:
            return amount * val / other_currency.btc_value
        return dec('0')

    def __repr__(self):
        return self.key
    __str__ = __repr__

    def __hash__(self):
        return self.key.__hash__()


class CurrencyKeeper(dict):
    __getattr__ = dict.__getitem__

    def __init__(self, currency_dictionary):
        super(CurrencyKeeper, self).__init__()
        self.version_lut = {}
        for key, config in currency_dictionary.iteritems():
            val = Currency(key, config)
            setattr(self, val.key, val)
            self.__setitem__(val.key, val)
            for ver in val.address_version:
                if ver in self.version_lut:
                    raise AttributeError("Duplicate address versions {}"
                                         .format(ver))
                self.version_lut[ver] = val

    def payout_currencies(self):
        return [c for c in self.itervalues() if c.exchangeable is True]

    def lookup_address(self, address):
        ver = address_version(address)
        try:
            return self.lookup_version(ver)
        except AttributeError:
            raise AttributeError("Address '{}' version {} is not a configured currency. Options are {}"
                                 .format(address, ver, self.available_versions))

    @property
    def available_versions(self):
        return {k: v.key for k, v in self.version_lut.iteritems()}

    def lookup_version(self, version):
        try:
            return self.version_lut[version]
        except KeyError:
            raise AttributeError(
                "Address version {} doesn't match available versions {}"
                .format(version, self.available_versions))


class ConfigurationException(Exception):
    pass


class RemoteException(Exception):
    pass


class PowerPool(object):
    timeout = 10
    requires = ['pp_location', 'monitor_port', 'stratum_port', 'sharechain_id',
                'title', 'diff', 'fee_perc']

    def __init__(self, bootstrap, algo):
        # Check requirements
        for req in self.requires:
            if req not in bootstrap:
                raise ConfigurationException("mining_servers item requires {}"
                                             .format(req))
        self.algo = algo
        # Default settings
        self.__dict__.update(dict())
        self.__dict__.update(bootstrap)

    @property
    def monitor_address(self):
        return "http://{}:{}".format(self.pp_location, self.monitor_port)

    @property
    def display_text(self):
        return self.stratum_address

    @property
    def hr_fee_perc(self):
        return float(self.fee_perc) * 100

    @property
    def dec_fee_perc(self):
        return dec(self.fee_perc)

    @property
    def stratum_address(self):
        return "stratum+tcp://{}:{}".format(self.pp_location, self.monitor_port)
    __repr__ = stratum_address  # Allows us to cache calls to this instance
    __str__ = stratum_address

    @property
    def port(self):
        return {'port': self.stratum_port, 'diff': self.diff,
                'title': self.title, 'fee': self.hr_fee_perc}

    def __hash__(self):
        return self.sharechain_id

    def request(self, url, method='GET', max_age=None, signed=True, **kwargs):
        url = urljoin(self.monitor_address, url)
        ret = requests.request(method, url, timeout=self.timeout, **kwargs)
        if ret.status_code != 200:
            raise RemoteException("Non 200 from remote: {}".format(ret.text))

        current_app.logger.debug("Got {} from remote".format(ret.text.encode('utf8')))
        return ret.json()


class PowerPoolKeeper(dict):
    def __init__(self, mining_servers):
        super(PowerPoolKeeper, self).__init__()
        self.by_algo = {}
        for algo, servers in mining_servers.iteritems():
            for serv_cfg in servers:
                serv = PowerPool(serv_cfg, algo)
                self.by_algo.setdefault(algo, [])
                self.by_algo[algo].append(serv)
                if serv.sharechain_id in self:
                    raise ConfigurationException("You cannot specify two servers "
                                                 "with the same sharechain_id")
                self[serv.sharechain_id] = serv

    @property
    def open_ports(self):
        open_ports = {}
        for algo, servers in self.by_algo.iteritems():
            for serv in servers:
                open_ports.setdefault(algo, [])
                open_ports[algo].append(serv.port)
        return open_ports


class ShareTracker(object):
    def __init__(self, algo):
        self.types = {typ: ShareTypeTracker(typ) for typ in ShareSlice.SHARE_TYPES}
        self.algo = current_app.config['algos'][algo]

    def count_slice(self, slc):
        self.types[slc.share_type].shares += slc.value

    @property
    def accepted(self):
        return self.types["acc"].shares

    @property
    def total(self):
        return sum([self.types['dup'].shares, self.types['low'].shares, self.types['stale'].shares, self.types['acc'].shares])

    def hashrate(self, typ="acc"):
        return self.types[typ].shares * self.algo['hashes_per_share']

    @property
    def rejected(self):
        return sum([self.types['dup'].shares, self.types['low'].shares, self.types['stale'].shares])

    @property
    def efficiency(self):
        rej = self.rejected
        if rej:
            return 100.0 * (float(self.types['acc'].shares) / rej)
        return 100.0


class ShareTypeTracker(object):
    def __init__(self, share_type):
        self.share_type = share_type
        self.shares = 0

    def __hash__(self):
        return self.share_type.__hash__()


def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()

        current_app.logger.info('{} (args {}, kwargs {}) in {}'
                                .format(method.__name__,
                                        args, kw, time_format(te - ts)))
        return result

    return timed


@cache.memoize(timeout=3600)
def get_pool_acc_rej(timedelta=None):
    """ Get accepted and rejected share count totals for the last month """
    if timedelta is None:
        timedelta = datetime.timedelta(days=30)

    # Pull from five minute shares if we're looking at a day timespan
    if timedelta <= datetime.timedelta(days=1):
        rej_typ = FiveMinuteReject
        acc_typ = FiveMinuteShare
    else:
        rej_typ = OneHourReject
        acc_typ = OneHourShare

    one_month_ago = datetime.datetime.utcnow() - timedelta
    rejects = (rej_typ.query.
               filter(rej_typ.time >= one_month_ago).
               filter_by(user="pool_stale"))
    accepts = (acc_typ.query.
               filter(acc_typ.time >= one_month_ago).
               filter_by(user="pool"))
    reject_total = sum([hour.value for hour in rejects])
    accept_total = sum([hour.value for hour in accepts])
    return reject_total, accept_total


@cache.memoize(timeout=3600)
def users_blocks(address, algo=None, merged=None):
    q = db.session.query(Block).filter_by(user=address, merged_type=None)
    if algo:
        q.filter_by(algo=algo)
    return algo.count()


@cache.memoize(timeout=86400)
def all_time_shares(address):
    shares = db.session.query(ShareSlice).filter_by(user=address)
    return sum([share.value for share in shares])


@cache.memoize(timeout=60)
def last_block_time(algo, merged_type=None):
    return last_block_time_nocache(algo, merged_type=merged_type)


def last_block_time_nocache(algo, merged_type=None):
    """ Retrieves the last time a block was solved using progressively less
    accurate methods. Essentially used to calculate round time.
    TODO XXX: Add pool selector to each of the share queries to grab only x11,
    etc
    """
    last_block = Block.query.filter_by(merged_type=merged_type, algo=algo).order_by(Block.height.desc()).first()
    if last_block:
        return last_block.found_at

    slc = ShareSlice.query.order_by(ShareSlice.time).first()
    if slc:
        return slc.time

    return datetime.datetime.utcnow()


@cache.memoize(timeout=60)
def last_block_share_id(currency, merged_type=None):
    return last_block_share_id_nocache(currency, merged_type=merged_type)


def last_block_share_id_nocache(algorithm=None, merged_type=None):
    last_block = Block.query.filter_by(merged_type=merged_type).order_by(Block.height.desc()).first()
    if not last_block or not last_block.last_share_id:
        return 0
    return last_block.last_share_id


@cache.memoize(timeout=60)
def last_block_found(algorithm=None, merged_type=None):
    last_block = Block.query.filter_by(merged_type=merged_type).order_by(Block.height.desc()).first()
    if not last_block:
        return None
    return last_block


def last_blockheight(merged_type=None):
    last = last_block_found(merged_type=merged_type)
    if not last:
        return 0
    return last.height


@cache.memoize(timeout=60)
def get_pool_hashrate(algo):
    """ Retrieves the pools hashrate average for the last 10 minutes. """
    lower, upper = make_upper_lower(offset=datetime.timedelta(minutes=1))
    ten_min = (ShareSlice.query.filter_by(user='pool', algo=algo)
               .filter(ShareSlice.time >= lower, ShareSlice.time <= upper))
    ten_min = sum([min.value for min in ten_min])
    # shares times hashes per n1 share divided by 600 seconds and 1000 to get
    # khash per second
    return float(ten_min) / 600000 * current_app.config['algos'][algo]['hashes_per_share']


@cache.memoize(timeout=30)
def get_round_shares(algo=None, merged_type=None):
    """ Retrieves the total shares that have been submitted since the last
    round rollover. """
    suffix = algo if algo else merged_type
    return sum(redis_conn.hvals('current_block_' + suffix)), datetime.datetime.utcnow()


def get_adj_round_shares(khashrate):
    """ Since round shares are cached we still want them to update on every
    page reload, so we extrapolate a new value based on computed average
    shares per second for the round, then add that for the time since we
    computed the real value. """
    round_shares, dt = get_round_shares()
    # # compute average shares/second
    now = datetime.datetime.utcnow()
    sps = float(khashrate * 1000)
    round_shares += int(round((now - dt).total_seconds() * sps))
    return round_shares, sps


@cache.cached(timeout=60, key_prefix='alerts')
def get_alerts():
    return yaml.load(open(root + '/static/yaml/alerts.yaml'))


@cache.memoize(timeout=60)
def last_10_shares(user, algo):
    lower, upper = make_upper_lower(offset=datetime.timedelta(minutes=1))
    minutes = (ShareSlice.query.filter_by(user=user).
               filter(ShareSlice.time > lower, ShareSlice.time < upper))
    if minutes:
        return sum([min.value for min in minutes])
    return 0


def collect_acct_items(address, limit=None, offset=0):
    """ Get account items for a specific user """
    payouts = (Payout.query.filter_by(user=address).join(Payout.block).
               order_by(Block.found_at.desc()).limit(limit).offset(offset))
    return payouts


def collect_user_stats(address):
    """ Accumulates all aggregate user data for serving via API or rendering
    into main user stats page """
    # store all the raw data of we're gonna grab
    workers = {}

    def check_new(address, worker, algo):
        """ Setups up an empty worker template. Since anything that has data on
        a worker can create one then it's useful to abstract. """
        key = (address, worker, algo)
        if key not in workers:
            workers[key] = {'total_shares': ShareTracker(algo),
                            'last_10_shares': ShareTracker(algo),
                            'online': False,
                            'servers': {},
                            'algo': algo,
                            'name': worker,
                            'address': address}
        return workers[key]

    # Get the lower bound for 10 minutes ago
    lower_10, upper_10 = make_upper_lower(offset=datetime.timedelta(minutes=1))

    newest = datetime.datetime.fromtimestamp(0)
    # XXX: Needs to only sum the last 24 hours
    for slc in ShareSlice.get_span(ret_query=True, user=(address, )):
        if slc.time > newest:
            newest = slc.time

        worker = check_new(slc.user, slc.worker, slc.algo)
        worker['total_shares'].count_slice(slc)
        if slc.time > lower_10:
            worker['last_10_shares'].count_slice(slc)

    hide_hr = newest < datetime.datetime.utcnow() - datetime.timedelta(seconds=current_app.config['worker_hashrate_fold'])

    # pull online status from cached pull direct from powerpool servers
    for worker, connection_summary in (cache.get('addr_online_' + address) or {}).iteritems():
        for ppid, connections in connection_summary.iteritems():
            try:
                powerpool = powerpools[ppid]
            except KeyError:
                continue

            worker = (address, worker, powerpool.algo)
            worker['online'] = True
            worker['servers'].setdefault(powerpool, 0)
            worker['servers'][powerpool] += 1

    # Could definitely be better... Makes a list of the dictionary keys sorted
    # by the worker name, then generates a list of dictionaries using the list
    # of keys
    workers = [workers[key] for key in sorted(workers.iterkeys(), key=lambda tpl: tpl[1])]
    settings = UserSettings.query.filter_by(user=address).first()

    # Generate payout history and stats for earnings all time
    earning_summary = {}
    def_earnings = dict(sent=dec('0'), earned=dec('0'), unconverted=dec('0'), immature=dec('0'), currency=None)
    # Go through already grouped aggregates
    aggregates = PayoutAggregate.query.filter_by(user=address).all()
    for aggr in aggregates:
        summary = earning_summary.setdefault(aggr.payout_currency, def_earnings.copy())
        if aggr.transaction_id:  # Mark sent if there's a txid attached
            summary['sent'] += aggr.amount
        else:
            summary['earned'] += aggr.amount

    # Loop through all unaggregated payouts to find the rest
    payouts = Payout.query.filter_by(user=address, aggregate_id=None).options(db.joinedload('block')).all()
    for payout in payouts:
        # Group by their desired payout currency
        summary = earning_summary.setdefault(payout.payout_currency, def_earnings.copy())

        # For non-traded values run an estimate calculation
        if payout.type == 1:  # PayoutExchange
            if not payout.block.mature:
                summary['immature'] += payout.est_value
            elif payout.block.mature and not payout.payable:
                summary['unconverted'] += payout.est_value
            else:
                summary['earned'] += aggr.final_amount
        else:
            if not payout.block.mature:
                summary['immature'] += payout.amount
            else:
                summary['earned'] += payout.amount

    # Set the currency as a value of the summary dictionary so we can convert
    # the dictionary of dictionaries into a list of dictionaries for rendering
    for currency in earning_summary:
        earning_summary[currency]['currency'] = currency

    # Show the user approximate next payout and exchange times
    now = datetime.datetime.now()
    next_exchange = now.replace(minute=0, second=0, microsecond=0, hour=((now.hour + 2) % 23))
    next_payout = now.replace(minute=0, second=0, microsecond=0, hour=0)

    f_perc = dec(current_app.config.get('fee_perc', dec('0.02'))) * 100

    return dict(workers=workers,
                payouts=payouts[:20],
                aggregates=aggregates[:20],
                settings=settings,
                next_payout=next_payout,
                earning_summary=earning_summary.values(),
                earning_summary_keys=def_earnings.keys(),
                hide_hr=hide_hr,
                next_exchange=next_exchange,
                f_per=f_perc)


def get_pool_eff(timedelta=None):
    rej, acc = get_pool_acc_rej(timedelta)
    # avoid zero division error
    if not rej and not acc:
        return 100
    else:
        return (float(acc) / (acc + rej)) * 100


def shares_to_hashes(shares):
    return float(current_app.config.get('hashes_per_share', 65536)) * shares


def resort_recent_visit(recent):
    """ Accepts a new dictionary of recent visitors and calculates what
    percentage of your total visits have gone to that address. Used to dim low
    percentage addresses. Also sortes showing most visited on top. """
    # accumulate most visited addr while trimming dictionary. NOT Python3 compat
    session['recent_users'] = []
    for i, (addr, visits) in enumerate(sorted(recent.items(), key=lambda x: x[1], reverse=True)):
        if i > 20:
            del recent[addr]
            continue
        session['recent_users'].append((addr, visits))

    # total visits in the list, for calculating percentage
    total = float(sum([t[1] for t in session['recent_users']]))
    session['recent_users'] = [(addr, (visits / total))
                               for addr, visits in session['recent_users']]


class Benchmark(object):
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = time.time()

    def __exit__(self, ty, val, tb):
        end = time.time()
        current_app.logger.info("BENCHMARK: {} in {}"
                                .format(self.name, time_format(end - self.start)))
        return False


def time_format(seconds):
    # microseconds
    if seconds <= 1.0e-3:
        return "{:,.4f} us".format(seconds * 1000000.0)
    if seconds <= 1.0:
        return "{:,.4f} ms".format(seconds * 1000.0)
    return "{:,.4f} sec".format(seconds)


def validate_bc_address(bc_address_str):
    """
    The go-to function for all your bitcoin style address validation needs.

    Expects to receive a string
    Returns False if any checks are failed, otherwise returns currency obj
    """
    # First check to make sure the address contains only alphanumeric chars
    if not bc_address_str.isalnum():
        return False

    # Check to make sure str is the proper length
    if not len(bc_address_str) == 34:
        return False

    # Check to see if the address can be looked up/has an attached RPC server
    try:
        curr = currencies.lookup_address(bc_address_str)
    except AttributeError:
        return False
    else:
        # If we've arrived here we'll consider it valid
        return curr


def validate_str_perc(perc, round=dec('0.01')):
    """
    The go-to function for all your percentage validation needs.

    Tries to convert a var representing an 0-100 scale percentage into a
    mathematically useful Python Decimal. Default is rounding to 0.01%

    Then checks to ensure decimal is within valid bounds
    """
    # Try to convert to decimal
    try:
        dec_perc = dec(perc).quantize(round) / 100
    except TypeError:
        return False
    else:
        # Check bounds
        if dec_perc > dec('1') or dec_perc < dec('0'):
            return False
        else:
            return dec_perc

##############################################################################
# Message validation and verification functions
##############################################################################
def validate_message_vals(address, **kwargs):
    set_addrs = kwargs['SET_ADDR']
    del_addrs = kwargs['DEL_ADDR']
    pdonate_perc = kwargs['SET_PDONATE_PERC']
    adonate_perc = kwargs['SET_ADONATE_PERC']
    adonate_addr = kwargs['SET_ADONATE_ADDR']
    del_adonate_addr = kwargs['DEL_ADONATE_ADDR']
    anon = kwargs['MAKE_ANON']

    # Make sure all addresses are valid
    for curr, addr in set_addrs.iteritems():
        if not validate_bc_address(addr):
            raise CommandException("Invalid {} address passed!".format(curr))

    if adonate_addr and not validate_bc_address(adonate_addr):
        raise CommandException("Invalid currency address passed for arbitrary "
                               "donation!")

    # Make sure all percentages are valid
    adonate_perc = validate_str_perc(adonate_perc)
    if adonate_perc is False:
        raise CommandException("Arbitrary donate percentage invalid! Check to "
                               "make sure its a value 0-100.")

    pdonate_perc = validate_str_perc(pdonate_perc)
    if pdonate_perc is False:
        raise CommandException("Pool donate percentage invalid! Check to "
                               "make sure its a value 0-100.")

    # Make sure percentages are <= 100
    if pdonate_perc + adonate_perc > 100:
        raise CommandException("Donation percentages cannot total to more than "
                               "100%!")

    # Make sure we have both an arb donate addr + an arb donate % or neither
    if not del_adonate_addr:
        if not adonate_perc >= 0 or adonate_addr is False:
            raise CommandException("Arbitrary donate requires both an address"
                                   "and a percentage, or to remove it both "
                                   "must be removed.")
    elif del_adonate_addr:
        if adonate_perc > 0 or adonate_addr:
            raise CommandException("Attempted to perform two conflicting "
                                   "actions with arbitrary donate! This is "
                                   "probably our fault - please contact us!")

    # Make sure arb donate addr isn't also the main addr
    if adonate_addr == address:
        raise CommandException("Arbitrary donate address must not be the "
                               "main address")

    return set_addrs, del_addrs, pdonate_perc, adonate_perc, adonate_addr, \
           del_adonate_addr, anon


def verify_message(address, curr, message, signature):
    update_dict = {'SET_ADDR': {}, 'DEL_ADDR': [], 'MAKE_ANON': False,
                   'SET_PDONATE_PERC': 0, 'SET_ADONATE_ADDR': False,
                   'SET_ADONATE_PERC': 0, 'DEL_ADONATE_ADDR': False}
    stamp = False
    site = False
    try:
        lines = message.split("\t")
        for line in lines:
            parts = line.split(" ")
            if parts[0] in update_dict:
                if parts[0] == 'SET_ADDR':
                    update_dict.setdefault(parts[0], {})
                    update_dict[parts[0]][parts[1]] = parts[2]
                elif parts[0] == 'DEL_ADDR':
                    update_dict[parts[0]].append(parts[1])
                else:
                    update_dict[parts[0]] = parts[1]
            elif parts[0] == 'Only':
                site = parts[3]
            elif parts[0] == 'Generated':
                time = float(parts[2])
                stamp = datetime.datetime.utcfromtimestamp(time)
            else:
                raise CommandException("Invalid command given! Generate a new "
                                       "message & try again.")
    except (IndexError, ValueError):
        current_app.logger.info("Invalid message provided", exc_info=True)
        raise CommandException("Invalid information provided in the message "
                               "field. This could be the fault of the bug with "
                               "IE11, or the generated message has an error")
    if not stamp:
        raise CommandException("Time stamp not found in message! Generate a new"
                               " message & try again.")

    now = datetime.datetime.utcnow()
    if abs((now - stamp).seconds) > current_app.config.get('message_expiry', 900):
        raise CommandException("Signature/Message is too old to be accepted! "
                               "Make sure your system clock is set correctly, "
                               "then generate a new message & try again.")

    if not site or site != current_app.config['site_title']:
        raise CommandException("Invalid website! Generate a new message "
                               "& try again.")

    current_app.logger.info(u"Attempting to validate message '{}' with sig '{}' for address '{}'"
                            .format(message, signature, address))

    args = validate_message_vals(address, **update_dict)

    try:
        res = curr.coinserv.verifymessage(address, signature, message.encode('utf-8').decode('unicode-escape'))
    except CoinRPCException as e:
        raise CommandException("Rejected by RPC server for reason {}!"
                               .format(e))
    except Exception:
        current_app.logger.error("Coinserver verification error!", exc_info=True)
        raise CommandException("Unable to communicate with coinserver!")

    if res:
        try:
            UserSettings.update(address, *args)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.error("Failed updating database with new user "
                                     "settings! Message: {}".format(message),
                                     exc_info=True)
            raise CommandException("Error saving new settings to the database!")
        else:
            db.session.commit()
    else:
        raise CommandException("Invalid signature! This is usually caused by"
                               "using the wrong address to sign the message or "
                               "not using the QT wallet. Coinserver returned {}"
                               .format(res))
