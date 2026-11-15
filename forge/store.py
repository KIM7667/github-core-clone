"""Deterministic scenario identities + the forge's in-memory store.

The suite builds every name it uses from one fixed seed (SCENARIO_SEED in the
kit's wire.ts). The forge re-derives the same identities so the login the
bearer token names matches the suite's viewer exactly. All timestamps come
from a fixed base (the trial's now when given) plus a logical counter, so two
runs on the same instance leave identical state.
"""
import os

U64_MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6a09e667f3bcc909

FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo', 'iris', 'jonas', 'kira', 'lars',
    'mira', 'nico', 'olga', 'pavel', 'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia', 'holm', 'iversen', 'jansen', 'keller',
    'lind', 'moreau', 'novak', 'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega', 'weber',
    'xu', 'yilmaz', 'zimmer',
]
REPO_ADJECTIVES = [
    'amber', 'brisk', 'calm', 'crisp', 'eager', 'faint', 'gentle', 'happy', 'idle', 'jolly', 'keen', 'lively',
    'mellow', 'nimble', 'open', 'plain', 'quick', 'quiet', 'rapid', 'soft', 'steady', 'tidy', 'upbeat',
    'vivid', 'warm', 'witty', 'young', 'zesty',
]
REPO_NOUNS = [
    'acorn', 'badger', 'canyon', 'cedar', 'comet', 'coral', 'delta', 'ember', 'falcon', 'glade', 'harbor',
    'indigo', 'juniper', 'kettle', 'lantern', 'meadow', 'nebula', 'orchid', 'pebble', 'quartz', 'raven',
    'saffron', 'thistle', 'umber', 'violet', 'willow', 'yarrow', 'zephyr',
]


def _fnv1a64(text):
    h = 0xcbf29ce484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001b3) & U64_MASK
    return h


def _mix64(value):
    z = (value + 0x9e3779b97f4a7c15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xbf58476d1ce4e5b9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94d049bb133111eb) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


class Rng:
    def __init__(self, seed):
        masked = seed & U64_MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545f4914f6cdd1d) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_rng(label):
    return Rng(_mix64(SCENARIO_SEED ^ _fnv1a64(label)))


def generate_users(count=12):
    rng = _scenario_rng('users')
    used = set()
    users = []
    for i in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = (first + last)
        login = base
        suffix = 2
        while login in used:
            login = "%s-%d" % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({"id": 1000 + i, "login": login,
                      "name": "%s %s" % (first, last),
                      "email": "%s@example.invalid" % login})
    return users


def generate_org():
    rng = _scenario_rng('org')
    return "%s-%s-org" % (rng.pick(REPO_ADJECTIVES), rng.pick(REPO_NOUNS))


USERS = generate_users()
VIEWER = USERS[0]
TEAMMATE = USERS[1]
STRANGER = USERS[2]
ORG = generate_org()

# Fixed base time: the trial's now when the env provides it, else the
# competition's opening. All created/updated stamps derive from it.
BASE_ISO = os.environ.get("MAKEFUN_TRIAL_NOW", "2026-11-14T06:30:00Z")
if not BASE_ISO.endswith("Z"):
    BASE_ISO += "Z"


class Clock:
    """Deterministic clock: every stamp is BASE_ISO; a counter only orders
    same-second events and never leaks wall time."""

    def __init__(self):
        self.n = 0

    def now(self):
        self.n += 1
        return BASE_ISO


class Repo:
    __slots__ = ("id", "owner", "name", "description", "private", "default_branch",
                 "refs", "objects", "issues", "pulls", "labels", "issue_seq", "pull_seq",
                 "comment_seq",
                 "created_at", "updated_at", "fork", "archived", "has_issues",
                 "has_projects", "has_wiki", "topics", "homepage", "language",
                 "watchers", "stargazers", "forks_count", "open_issues_count")

    def __init__(self, rid, owner, name, description="", private=False, default_branch="main"):
        self.id = rid
        self.owner = owner
        self.name = name
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.refs = {}          # refname -> sha hex (e.g. refs/heads/main)
        self.objects = {}       # sha -> (otype, raw bytes)
        self.issues = {}        # number -> dict
        self.pulls = {}         # number -> dict
        self.labels = {}        # name -> dict
        self.issue_seq = 0
        self.pull_seq = 0
        self.comment_seq = 0
        self.created_at = BASE_ISO
        self.updated_at = BASE_ISO
        self.fork = False
        self.archived = False
        self.has_issues = True
        self.has_projects = True
        self.has_wiki = True
        self.topics = []
        self.homepage = None
        self.language = None
        self.watchers = 0
        self.stargazers = 0
        self.forks_count = 0
        self.open_issues_count = 0


class Store:
    def __init__(self):
        self.repos = {}         # (owner, name) -> Repo
        self.repo_seq = 0
        self.clock = Clock()
        self._id_seq = 100000
        self._order_seq = 0
        # issue/pull global counters are per-repo in GitHub; ids are per-repo numbers.
        # Global node ids derive from repo id + number, so they are stable.

    def next_id(self):
        self._id_seq += 1
        return self._id_seq

    def next_seq(self):
        self._order_seq += 1
        return self._order_seq

    def create_repo(self, owner, name, description="", private=False, default_branch="main"):
        self.repo_seq += 1
        repo = Repo(self.repo_seq, owner, name, description, private, default_branch)
        self.repos[(owner, name)] = repo
        return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        return self.repos.pop((owner, name), None)

    def repos_for_owner(self, owner):
        return [r for (o, _), r in sorted(self.repos.items()) if o == owner]

    def all_repos(self):
        return [self.repos[k] for k in sorted(self.repos.keys())]
