"""
GluttonousTypr — v9.6
Global autocorrect + deep-learning prediction for Windows.

Requires: pip install pynput symspellpy transformers torch pystray pillow
          pygetwindow language-tool-python pywin32 onnxruntime
          "optimum[onnxruntime]" peft datasets

Hotkeys:
  Ctrl+Shift+A   toggle autocorrect
  Ctrl+Shift+P   toggle prediction
  Ctrl+Space     accept prediction
  Shift+`        undo last correction (within 5 seconds)
  Ctrl+Shift+G   grammar check
  Ctrl+Shift+H   show hotkeys
  Ctrl+Shift+L   LoRA fine-tune
"""

# ============================================================
#  EARLY CONSOLE DETECTION
# ============================================================
import sys

_HAS_CONSOLE = (
    sys.stdout is not None
    and hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
)
_NO_STDOUT = sys.stdout is None
_NO_STDERR = sys.stderr is None


class _NullWriter:
    def write(self, *a, **k): pass
    def flush(self, *a, **k): pass
    def isatty(self): return False
    def fileno(self): raise OSError("no console")


if _NO_STDOUT:
    sys.stdout = _NullWriter()
if _NO_STDERR:
    sys.stderr = _NullWriter()


# ============================================================
#  STANDARD IMPORTS
# ============================================================
import atexit
import ctypes
import ctypes.wintypes as wintypes
import importlib.resources
import json
import logging
import logging.handlers
import os
import pathlib
import re
import shutil
import signal
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, deque

from pynput import keyboard
from symspellpy import SymSpell, Verbosity

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False

try:
    import pygetwindow as gw
    _HAS_GETWINDOW = True
except ImportError:
    _HAS_GETWINDOW = False


# ============================================================
#  PATHS + LOGGING
# ============================================================
APP_DIR = pathlib.Path.home() / ".gluttonoustypr"
APP_DIR.mkdir(exist_ok=True)

_old_dir = pathlib.Path.home() / ".autocorrect"
if _old_dir.exists() and not (APP_DIR / ".migrated").exists():
    try:
        for item in _old_dir.iterdir():
            dest = APP_DIR / item.name
            if not dest.exists():
                try:
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                except Exception:
                    pass
        (APP_DIR / ".migrated").touch()
    except Exception:
        pass

LOG_FILE               = APP_DIR / "gluttonoustypr.log"
PERSONAL_DICT_FILE     = APP_DIR / "personal_dict.json"
TYPO_CACHE_FILE        = APP_DIR / "common_typos.json"
WRONG_CORRECTIONS_FILE = APP_DIR / "wrong_corrections.json"
NAME_WHITELIST_FILE    = APP_DIR / "names.txt"
APP_CONTEXT_DIR        = APP_DIR / "app_contexts"
LORA_ADAPTER_DIR       = APP_DIR / "lora_adapter"
LORA_DATA_FILE         = APP_DIR / "personal_corpus.txt"

APP_CONTEXT_DIR.mkdir(exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logger = logging.getLogger("gluttonoustypr")
logger.setLevel(logging.DEBUG)
logger.addHandler(_handler)

if _HAS_CONSOLE:
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_console_handler)

logger.info("=" * 50)
logger.info("GluttonousTypr v9.6 starting")
logger.info(f"Console mode: {'yes' if _HAS_CONSOLE else 'no (background)'}")


# ============================================================
#  HIDE CONSOLE WINDOW
# ============================================================
def hide_console_window():
    if not _HAS_CONSOLE:
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


# ============================================================
#  WINDOWS API
# ============================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

ES_PASSWORD = 0x0020
GWL_STYLE = -16
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wintypes.DWORD),
        ("flags",         wintypes.DWORD),
        ("hwndActive",    wintypes.HWND),
        ("hwndFocus",     wintypes.HWND),
        ("hwndCapture",   wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize",  wintypes.HWND),
        ("hwndCaret",     wintypes.HWND),
        ("rcCaret",       wintypes.RECT),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _get_focused_control():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not tid:
        return None
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return None
    return gti.hwndFocus


_pwd_cache = {"time": 0.0, "value": False}


def is_password_field():
    now = time.time()
    if now - _pwd_cache["time"] < 0.5:
        return _pwd_cache["value"]
    result = False
    try:
        hwnd = _get_focused_control()
        if hwnd:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            if cls_buf.value == "Edit":
                style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                result = bool(style & ES_PASSWORD)
    except Exception as e:
        logger.debug(f"Password detection error: {e}")
    _pwd_cache["time"] = now
    _pwd_cache["value"] = result
    return result


def get_active_process_name():
    """Return the lowercase exe name of the foreground window (e.g. 'code.exe')."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            if not ok:
                return ""
            return pathlib.Path(buf.value).name.lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def get_focused_control_class():
    try:
        hwnd = _get_focused_control()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def is_fullscreen_window():
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        return (rect.right - rect.left) >= w and (rect.bottom - rect.top) >= h
    except Exception:
        return False


# ============================================================
#  DEEP LEARNING MODEL
# ============================================================
_model = None
_tokenizer = None
_model_ready = threading.Event()
_use_onnx = False


def _load_dl_model():
    global _model, _tokenizer, _use_onnx
    try:
        logger.info("Loading DistilGPT-2 (first run downloads ~330 MB)...")
        from transformers import GPT2TokenizerFast, GPT2LMHeadModel
        _tokenizer = GPT2TokenizerFast.from_pretrained("distilgpt2")
        try:
            from optimum.onnxruntime import ORTModelForCausalLM
            _model = ORTModelForCausalLM.from_pretrained("distilgpt2", export=True)
            _use_onnx = True
            logger.info("ONNX Runtime model loaded.")
        except Exception as e:
            logger.warning(f"ONNX unavailable ({e}); using PyTorch")
            import torch
            torch.set_num_threads(4)
            _model = GPT2LMHeadModel.from_pretrained("distilgpt2")
            _model.eval()
        logger.info(f"DistilGPT-2 ready (ONNX={_use_onnx}).")
    except Exception as e:
        logger.error(f"Deep learning unavailable: {e}")
    finally:
        _model_ready.set()


def _start_model_loading():
    threading.Thread(target=_load_dl_model, daemon=True).start()


# ============================================================
#  SPELL CHECKER
# ============================================================
sym_spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
_dict_path = importlib.resources.files("symspellpy") / "frequency_dictionary_en_82_765.txt"
_bigram_path = importlib.resources.files("symspellpy") / "frequency_bigramdictionary_en_243_342.txt"
sym_spell.load_dictionary(str(_dict_path), term_index=0, count_index=1)
sym_spell.load_bigram_dictionary(str(_bigram_path), term_index=0, count_index=2)
logger.info(f"Dictionary: {sym_spell.word_count:,} words")

_known_words = set()
try:
    with open(str(_dict_path), encoding="utf-8") as _f:
        for _line in _f:
            _parts = _line.strip().split()
            if _parts:
                _known_words.add(_parts[0].lower())
except Exception:
    pass


def _is_known_word(w):
    return w.lower() in _known_words


# ============================================================
#  LOOKUP TABLES
# ============================================================
KEYBOARD_ADJACENT = {
    "teh": "the", "hte": "the", "adn": "and", "nad": "and",
    "taht": "that", "htat": "that", "thta": "that",
    "fo": "of", "ot": "to", "wiht": "with",
    "soem": "some", "jsut": "just", "liek": "like",
    "thsi": "this", "tihs": "this",
    "waht": "what", "whta": "what",
    "fro": "for", "form": "from", "fomr": "from",
    "cna": "can", "tkae": "take", "mkae": "make",
    "tiem": "time", "nit": "not", "nte": "net",
    "htere": "there", "alwyas": "always", "woudl": "would",
}

FALLBACK_TYPOS = {
    "teh": "the", "hte": "the", "adn": "and", "nad": "and",
    "taht": "that", "htat": "that", "fo": "of", "ot": "to",
    "wiht": "with", "soem": "some",
    "absense": "absence", "abcense": "absence", "absance": "absence",
    "acceptible": "acceptable", "accidently": "accidentally",
    "accomodate": "accommodate", "acheive": "achieve",
    "acknowlege": "acknowledge", "aknowledge": "acknowledge",
    "aquaintance": "acquaintance", "aquire": "acquire", "adquire": "acquire",
    "aquit": "acquit", "acrage": "acreage", "acerage": "acreage",
    "adress": "address", "adultary": "adultery", "adviseable": "advisable",
    "agression": "aggression", "agressive": "aggressive",
    "allmost": "almost", "amatuer": "amateur",
    "amature": "amateur", "anually": "annually", "annualy": "annually",
    "apparant": "apparent", "aparent": "apparent", "artic": "arctic",
    "arguement": "argument", "calender": "calendar", "camoflage": "camouflage",
    "camoflague": "camouflage", "carribean": "caribbean",
    "catagory": "category", "cauhgt": "caught", "cemetary": "cemetery",
    "changable": "changeable", "cheif": "chief", "collegue": "colleague",
    "colum": "column", "comming": "coming", "commited": "committed",
    "conscence": "conscience", "consciencious": "conscientious",
    "consious": "conscious", "concensus": "consensus",
    "convienient": "convenient", "definitly": "definitely",
    "definately": "definitely", "disipline": "discipline",
    "embarass": "embarrass", "enviroment": "environment",
    "equiptment": "equipment", "existance": "existence",
    "experiance": "experience", "familar": "familiar", "finaly": "finally",
    "foriegn": "foreign", "freind": "friend", "goverment": "government",
    "grammer": "grammar", "garantee": "guarantee", "gurantee": "guarantee",
    "heigth": "height", "humerous": "humorous", "immediatly": "immediately",
    "independant": "independent", "indispensible": "indispensable",
    "inteligence": "intelligence", "knowlege": "knowledge", "libary": "library",
    "maintainance": "maintenance", "millenium": "millennium",
    "neccessary": "necessary", "noticable": "noticeable",
    "occassion": "occasion", "occasionaly": "occasionally",
    "occured": "occurred", "occurence": "occurrence", "occurance": "occurrence",
    "parrallel": "parallel", "perticular": "particular", "passtime": "pastime",
    "personel": "personnel", "posess": "possess", "posession": "possession",
    "prefered": "preferred", "predjudice": "prejudice", "priviledge": "privilege",
    "publically": "publicly", "questionaire": "questionnaire",
    "recieve": "receive", "recieved": "received", "recomend": "recommend",
    "refered": "referred", "relevent": "relevant", "restaraunt": "restaurant",
    "rythm": "rhythm", "shedule": "schedule", "seperate": "separate",
    "seperation": "separation", "similiar": "similar", "sincerly": "sincerely",
    "succede": "succeed", "succesful": "successful", "supercede": "supersede",
    "suprise": "surprise", "temperture": "temperature", "tendancy": "tendency",
    "threshhold": "threshold", "tommorow": "tomorrow", "tounge": "tongue",
    "truely": "truly", "unfortunatly": "unfortunately", "untill": "until",
    "vaccuum": "vacuum", "vehical": "vehicle", "wierd": "weird",
    "beleive": "believe", "buisness": "business", "concious": "conscious",
    "alright": "all right", "infront": "in front",
    "aswell": "as well", "incase": "in case", "eachother": "each other",
    "noone": "no one", "everytime": "every time", "atleast": "at least",
    "dont": "don't", "cant": "can't", "wont": "won't",
    "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't",
    "werent": "weren't", "doesnt": "doesn't", "didnt": "didn't",
    "couldnt": "couldn't", "shouldnt": "shouldn't", "wouldnt": "wouldn't",
    "youre": "you're", "youve": "you've", "youll": "you'll",
    "theyre": "they're", "theyve": "they've", "weve": "we've",
    "thats": "that's", "whats": "what's", "theres": "there's",
    "heres": "here's", "lets": "let's", "hes": "he's", "shes": "she's",
}

common_typos = dict(FALLBACK_TYPOS)

for _risky in ("its", "were", "well", "id", "ill", "im", "ive",
               "ya", "dat", "dis", "yall"):
    common_typos.pop(_risky, None)

CONFUSED_WORDS = {
    "affect":   {"verb": "affect",   "noun": "effect",   "hint": "verb=influence"},
    "effect":   {"verb": "affect",   "noun": "effect",   "hint": "noun=result"},
    "then":     {"comparison": "than", "time": "then"},
    "than":     {"comparison": "than", "time": "then"},
    "accept":   {"receive": "accept", "exclude": "except"},
    "except":   {"receive": "accept", "exclude": "except"},
    "advice":   {"noun": "advice",   "verb": "advise"},
    "advise":   {"noun": "advice",   "verb": "advise"},
    "lose":     {"verb": "lose",     "adjective": "loose"},
    "loose":    {"verb": "lose",     "adjective": "loose"},
    "passed":   {"verb": "passed",   "preposition": "past"},
    "past":     {"verb": "passed",   "preposition": "past"},
    "precede":  {"come_before": "precede", "go_forward": "proceed"},
    "proceed":  {"come_before": "precede", "go_forward": "proceed"},
}

SLANG_WHITELIST = {
    "yo", "yoyo", "sup", "wassup", "wazzup", "whassup", "whatup", "waddup",
    "hey", "heya", "heyy", "heyyy", "hii", "hiii", "yooo", "yoooo",
    "homie", "homey", "homies", "bro", "broski", "bruh", "bruhh", "brah",
    "brudda", "bredren", "bredrin", "fam", "famalam", "cuz", "cuzzo",
    "dawg", "dawgs", "g", "gee", "playa", "player", "pimp", "boss",
    "chief", "king", "queen", "sis", "sista", "brotha", "brutha", "sistah",
    "gurl", "girl", "boo", "bae", "babe", "baby", "shawty", "shorty",
    "shordy", "shawtys", "shorties", "mami", "papi", "mijo", "mija",
    "homes", "homeslice", "homiette", "peeps", "peepz", "folks", "folkz",
    "word", "bet", "facts", "fax", "nocap", "ong", "fr", "frfr", "ongg",
    "sheesh", "sheeeesh", "damn", "dang", "darn", "dag", "dayum", "dayumm",
    "yikes", "oof", "oops", "welp", "whelp", "meh", "eh", "ehh",
    "ugh", "ughh", "ew", "eww", "ewww", "ick", "yuck", "yucko",
    "yay", "yayy", "woohoo", "woot", "yesss", "yessir", "yesmaam",
    "nooo", "noooo", "nah", "naw", "naww", "nope", "nopers",
    "huh", "hmph", "hmm", "hmmm", "hmmmm", "mmm", "mmmm", "mhm",
    "pfft", "psh", "pshaw", "tch",
    "lol", "lmao", "lmfao", "rofl", "rotfl", "haha", "hahaha",
    "hehe", "hehehe", "hihi", "lul", "lulz", "kek",
    "omg", "omfg", "wtf", "wth", "tf", "stfu", "gtfo", "smh", "foh",
    "rip", "wow", "woah", "whoa", "shiiit", "shiii",
    "dangit", "goshdarnit", "gosh", "jeez", "jeeze", "geez",
    "jesus", "lordy", "lord", "lawd", "lawdy",
    "dope", "fire", "lit", "litt", "litty", "bussin",
    "slaps", "slappin", "bangs", "bangin", "crackin",
    "tight", "sick", "wicked", "ill", "killer", "killin", "kilt",
    "raw", "hard", "clutch", "goated", "goat", "goted",
    "mid", "mids", "trash", "garbage", "whack", "bunk",
    "snatched", "slay", "slayy", "slayed", "slaying",
    "periodt", "period", "yass", "yasss", "queen", "werk",
    "sus", "sussy", "cringe", "cringey", "cringy",
    "basic", "thirsty", "thot", "thottie", "hoe", "hoes",
    "clown", "clownin", "buffoon", "bozo", "dingus", "dingbat", "dodo",
    "dummy", "dumdum", "numbskull", "numpty", "knucklehead", "blockhead",
    "sucker", "punk", "chump", "jerk", "jerkface", "jackass",
    "jabroni", "scrub", "bum", "bums", "hater", "haters", "hatin",
    "fuckboy", "fboi", "fuccboi", "simp", "simps", "simpy", "simping",
    "incel", "incels", "karen", "karens", "chad", "chads",
    "neckbeard", "neckbeards", "noob", "newb", "newbie", "n00b",
    "booboo", "babygirl", "babyboy", "wifey", "hubby", "main", "mainthing",
    "sidepiece", "sidechick", "hookup", "hookups",
    "roast", "roasted", "roasting", "dissing", "dis", "disses",
    "beefing", "beefin", "beef", "beefs", "shade", "shady", "petty",
    "messy", "messiness", "drama", "dramatic", "dramaqueen",
    "squad", "squads", "crew", "crews", "posse", "clique", "gang",
    "tribe", "tribes", "squadup", "squaddeep", "squadgoals",
    "rideordie", "bestie", "besties", "bff", "bffs",
    "roomie", "roomies", "broham", "brohams",
    "chill", "chillax", "chillin", "chillen", "vibin", "vibing",
    "vibe", "vibes", "vibey", "goodvibes",
    "hang", "hangin", "hangout", "chilling", "kickin", "kickinit",
    "bounce", "bounced", "dip", "dippin", "dipped", "peacing", "ghosting",
    "ghosted", "ghostin", "flex", "flexin", "flexing", "flexes", "flexed",
    "stunt", "stuntin", "stunts", "ball", "ballin", "baller",
    "grind", "grindin", "grinding", "hustle", "hustlin", "hustler",
    "scheme", "schemin", "plottin", "plotting", "cappin", "cap", "caps",
    "frontin", "fronting", "sippin", "slippin",
    "trippin", "buggin", "bugging", "wildin", "wilding", "geekin",
    "geeking", "hypin", "hyping", "biting", "bite", "bit",
    "cookin", "cooking", "burning", "burnt", "gassin", "gassed",
    "smashing", "smash", "smashin", "piping", "hittin", "hit",
    "throwin", "throwing", "catching", "caught", "pulling", "pullup",
    "cheddar", "paper", "papers", "bands", "bandz", "racks", "rackz",
    "guap", "gwap", "moolah", "moola", "scratch", "bread", "dough",
    "coin", "coins", "cashflow", "bigmoney", "bag", "bags",
    "broke", "brokeboi", "brokeboy", "rich", "wealthy", "moneyed",
    "dank", "danks", "gas", "za", "zaza", "pack", "packs",
    "loud", "loudpack", "tree", "trees", "herb", "herbs", "bud", "buds",
    "green", "greens", "exotic", "exotics",
    "stoned", "blazed", "faded", "fried", "toasted",
    "roasted", "baked", "cooked", "gassed", "zoned", "zonedout",
    "trap", "trapping", "drill", "drilling", "drip", "dripping", "drippy",
    "sauce", "saucy", "beat", "beats", "flow",
    "bars", "punchline", "punchlines", "freestyle", "cypher", "cyphers",
    "mixtape", "mixtapes", "hook", "hooks", "verse", "verses",
    "disstrack", "clapback", "clapbacks",
    "spitting", "spit", "spittin", "wildnout",
    "pog", "pogs", "poggers", "pogchamp", "omegalul", "monkas", "pepehands",
    "ez", "ezz", "ezclap", "gg", "ggs", "ggwp", "glhf", "op", "nerf",
    "buff", "meta", "pwnd", "pwned", "rekt",
    "salty", "raging", "tilted", "tilt", "sweat", "sweaty", "sweats",
    "tryhard", "tryhards", "carry", "carried", "feeder", "feed",
    "clutched", "owned", "owning",
    "yeet", "yeeted", "yeeting", "yoink", "yoinked", "yoinking",
    "smol", "smoll", "chonk", "chonky", "boop", "booped",
    "derp", "derpy", "derped", "derping", "noms", "nomnom",
    "uwu", "owo", "uvu", "rawr", "nya", "nyaa", "meowdy",
    "yall", "yalls", "yins", "yinz", "youse", "yous",
    "aint", "gonna", "wanna", "gotta", "finna",
    "tryna", "shoulda", "coulda", "woulda", "musta", "hafta",
    "lemme", "gimme", "dunno", "dontcha", "didntcha",
    "cantcha", "wontcha", "fixin", "fixing", "yonder",
    "howdy", "howdies",
    "yanno", "yaknow", "yakno", "yuh", "yea", "yeah", "yep", "yup", "yupp",
    "yah", "naw", "nah", "blah", "anyways", "anywho", "anywayz",
    "whatever", "whatevs", "whatev",
    "finsta", "chile", "chilee", "chyle", "gworl", "gworls",
    "sisses", "brothas", "sistas", "aight", "aiight", "igh", "ight",
    "dassit", "dasit", "datsit", "dass", "dat", "dese", "doe",
    "gwan", "trynna", "tryin", "bougie", "boujee", "bouj", "boujie",
    "ratchet", "ghetto", "ghettos", "chie", "honey", "hon",
    "hun", "hunn", "hunnies", "honeyy", "shug", "sugar",
    "lawdd", "lordt", "lordhamercy", "gawd", "gawwd", "gawdd",
    "gawt", "gawta", "gawtcha", "gon", "gone", "gonbe", "gonn",
    "ya", "yaa",
    "hella", "helluva", "hecka",
    "lowkey", "highkey", "deadass", "realtalk",
    "tbh", "ngl", "imo", "imho", "fwiw", "btw", "brb", "afk",
    "irl", "tmi", "ttyl", "ttys", "hmu", "hitmeup",
    "dm", "dms", "dmed", "dming", "sliding", "slid", "slide",
    "rizz", "rizzed", "rizzing", "rizzler",
    "gyatt", "gyat", "skibidi", "skibiditoilet",
    "sigma", "sigmas", "sigmagrindset", "sigmamale",
    "fanum", "fanumtax", "mewing", "mew", "mewed",
    "looksmaxxing", "looksmax", "gymmaxxing",
    "edging", "edged", "gooner", "gooners", "gooning", "goon",
    "npc", "npcs", "npcenergy", "npcstream",
    "delulu", "delulus", "delululand",
    "chronicallyonline", "terminallyonline",
    "brainrot", "brainrotted", "brainrotmaxxing",
    "glazing", "glazed", "glazer", "glazers", "glaze",
    "aura", "auras", "aurafarming", "aurapoints",
    "crashout", "crashouts", "crashedout",
    "lockedin", "ate", "atee", "serve", "served", "serving",
    "mother", "mothered", "mothering", "eating", "eats",
    "purr", "meow", "meows", "meowed", "meowing",
    "ijbol", "pmo", "pmos", "kye",
}

SLANG_REPLACEMENTS = {
    "yoe": "yo", "yoo": "yo",
    "homi": "homie", "homei": "homie", "homiie": "homie",
    "homeez": "homies", "homiez": "homies",
    "brahh": "brah",
    "wasup": "wassup", "watzup": "wassup", "watsup": "wassup",
    "whassup": "wassup", "wadup": "waddup",
    "waddap": "waddup", "wazzup": "wassup",
    "cuzz": "cuz", "cuzzo": "cuz", "cuzo": "cuz",
    "dawgg": "dawg", "dawwg": "dawg", "dawgz": "dawgs",
    "damm": "damn", "dyam": "dayum",
    "dangg": "dang", "danggg": "dang",
    "sheeesh": "sheesh", "sheeeshh": "sheesh",
    "yikess": "yikes", "yikesss": "yikes",
    "welpp": "welp", "mehh": "meh",
    "ughh": "ugh", "ughhh": "ugh",
    "eew": "ew", "ewww": "ew",
    "yayyy": "yay", "yayyyy": "yay",
    "noooooo": "no", "nooo": "no",
    "nawww": "naw", "naww": "naw",
    "hmmm": "hmm", "hmmmm": "hmm",
    "loll": "lol", "lolz": "lol",
    "lmaoo": "lmao", "lmfaoo": "lmfao",
    "hahah": "haha", "hahahah": "haha",
    "heheh": "hehe",
    "omgg": "omg", "omggg": "omg",
    "wtff": "wtf",
    "damnnn": "damn", "dayumm": "dayum",
    "bruhh": "bruh", "bruhhh": "bruh",
    "brooo": "bro", "broooo": "bro",
    "siss": "sis", "sistahh": "sistah",
    "famalam": "fam", "famm": "fam", "fammm": "fam",
}


# ============================================================
#  HOMOPHONE (opt-in)
# ============================================================
ENABLE_HOMOPHONE = False
HOMOPHONE_GROUPS = [
    {"their", "there", "they're"},
    {"your", "you're"},
    {"its", "it's"},
    {"then", "than"},
    {"affect", "effect"},
    {"lose", "loose"},
    {"weather", "whether"},
    {"whose", "who's"},
    {"passed", "past"},
    {"to", "too", "two"},
    {"accept", "except"},
    {"advice", "advise"},
    {"aloud", "allowed"},
    {"brake", "break"},
    {"buy", "by", "bye"},
    {"cereal", "serial"},
    {"complement", "compliment"},
    {"council", "counsel"},
    {"desert", "dessert"},
    {"fare", "fair"},
    {"forth", "fourth"},
    {"hear", "here"},
    {"knew", "new"},
    {"know", "no"},
    {"lie", "lay"},
    {"meat", "meet"},
    {"peace", "piece"},
    {"plain", "plane"},
    {"principal", "principle"},
    {"right", "write"},
    {"scene", "seen"},
    {"stationary", "stationery"},
    {"threw", "through"},
    {"wait", "weight"},
    {"weak", "week"},
    {"wear", "where"},
    {"which", "witch"},
    {"whole", "hole"},
]
_HOMOPHONE_INDEX = {}
for _g in HOMOPHONE_GROUPS:
    for _w in _g:
        _HOMOPHONE_INDEX[_w] = _g


def disambiguate_homophone(word, sentence_prefix):
    if not ENABLE_HOMOPHONE:
        return None
    lower = word.lower().rstrip(".,!?;:")
    group = _HOMOPHONE_INDEX.get(lower)
    if not group or len(lower) < 4:
        return None
    if not _model_ready.is_set() or _model is None:
        return None
    try:
        import torch
        best_word, best_score = lower, float("-inf")
        for cand in group:
            test = (sentence_prefix + " " + cand).strip()
            inp = _tokenizer(test, return_tensors="pt")
            if _use_onnx:
                out = _model(**inp)
            else:
                with torch.no_grad():
                    out = _model(**inp)
            score = out.logits[0, -1, :].max().item()
            if score > best_score:
                best_score = score
                best_word = cand
        return best_word if best_word != lower else None
    except Exception:
        return None


_SPECIAL_CONTRACTIONS = {
    "im": "I'm", "ive": "I've", "ill": "I'll", "id": "I'd",
    "youre": "you're", "youve": "you've", "youll": "you'll", "youd": "you'd",
    "weve": "we've", "theyre": "they're", "theyve": "they've", "theyll": "they'll",
    "hes": "he's", "shes": "she's", "aint": "ain't",
}


# ============================================================
#  PREDICTION
# ============================================================
def predict_next_words():
    if not _model_ready.is_set() or _model is None:
        return []
    with state_lock:
        if not context_words:
            return []
        prompt = " ".join(list(context_words)[-PREDICTION_CONTEXT:])
    app_hint = _get_app_context()
    for key, prefix in APP_HINTS.items():
        if key in app_hint and prefix:
            prompt = prefix + prompt
            break
    try:
        import torch
        inp = _tokenizer(prompt, return_tensors="pt")
        if _use_onnx:
            out = _model(**inp)
        else:
            with torch.no_grad():
                out = _model(**inp)
        logits = out.logits[0, -1, :]
        topk = torch.topk(logits, PREDICTION_TOP_K)
        ids = topk.indices.tolist()
        vals = topk.values.tolist()
        if len(vals) >= 2 and (vals[0] - vals[1]) < CONFIDENCE_GATE:
            return []
        out_words, seen = [], set()
        for tid in ids:
            w = _tokenizer.decode([tid]).strip().lower()
            if w and all(c.isalpha() or c == "'" for c in w) and len(w) >= 2:
                if w not in seen:
                    seen.add(w)
                    out_words.append(w)
        return out_words
    except Exception as e:
        logger.debug(f"Prediction error: {e}")
        return []


def predict_multiword(context_text, max_new_tokens=3):
    if not _model_ready.is_set() or _model is None:
        return ""
    if not context_text.strip():
        return ""
    try:
        import torch
        inp = _tokenizer(context_text, return_tensors="pt")
        if _use_onnx:
            gen = _model.generate(
                **inp, max_new_tokens=max_new_tokens, do_sample=False,
                num_beams=1, pad_token_id=_tokenizer.eos_token_id,
            )
        else:
            with torch.no_grad():
                gen = _model.generate(
                    **inp, max_new_tokens=max_new_tokens, do_sample=False,
                    num_beams=1, pad_token_id=_tokenizer.eos_token_id,
                )
        prompt_len = inp["input_ids"].shape[1]
        new_ids = gen[0][prompt_len:]
        return _tokenizer.decode(new_ids, skip_special_tokens=True).strip()
    except Exception as e:
        logger.debug(f"Multi-word error: {e}")
        return ""


# ============================================================
#  PERSISTENCE
# ============================================================
personal_freq = Counter()
wrong_corrections = Counter()
name_whitelist = set()
app_contexts = {}
_save_lock = threading.Lock()


def load_personal_data():
    global personal_freq, wrong_corrections, name_whitelist, app_contexts
    if PERSONAL_DICT_FILE.exists():
        try:
            personal_freq.update(json.loads(PERSONAL_DICT_FILE.read_text(encoding="utf-8")))
            logger.info(f"Loaded {len(personal_freq)} personal words")
        except Exception as e:
            logger.error(f"personal_dict load failed: {e}")
    if WRONG_CORRECTIONS_FILE.exists():
        try:
            wrong_corrections.update(json.loads(WRONG_CORRECTIONS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            logger.error(f"wrong_corrections load failed: {e}")
    if NAME_WHITELIST_FILE.exists():
        try:
            for line in NAME_WHITELIST_FILE.read_text(encoding="utf-8").splitlines():
                w = line.strip()
                if w and not w.startswith("#"):
                    name_whitelist.add(w.lower())
            logger.info(f"Whitelist: {len(name_whitelist)} names")
        except Exception as e:
            logger.error(f"whitelist load failed: {e}")
    for ctx_file in APP_CONTEXT_DIR.glob("*.json"):
        try:
            data = json.loads(ctx_file.read_text(encoding="utf-8"))
            app_contexts[ctx_file.stem] = {
                "words": Counter(data.get("words", {})),
                "snippets": data.get("snippets", []),
            }
        except Exception:
            pass
    logger.info(f"Loaded {len(app_contexts)} app contexts")


def save_personal_data():
    with _save_lock:
        try:
            PERSONAL_DICT_FILE.write_text(
                json.dumps(dict(personal_freq), indent=0), encoding="utf-8",
            )
            WRONG_CORRECTIONS_FILE.write_text(
                json.dumps(dict(wrong_corrections), indent=0), encoding="utf-8",
            )
            for app_name, ctx in app_contexts.items():
                (APP_CONTEXT_DIR / f"{app_name}.json").write_text(
                    json.dumps({
                        "words": dict(ctx["words"]),
                        "snippets": ctx["snippets"][-100:],
                    }, indent=0),
                    encoding="utf-8",
                )
            logger.debug("Personal data saved")
        except Exception as e:
            logger.error(f"Save failed: {e}")


# ============================================================
#  COMMON TYPOS FROM INTERNET
# ============================================================
def fetch_common_typos():
    global common_typos
    if TYPO_CACHE_FILE.exists():
        try:
            cached = json.loads(TYPO_CACHE_FILE.read_text(encoding="utf-8"))
            new_keys = {k: v for k, v in cached.items() if k not in FALLBACK_TYPOS}
            common_typos.update(new_keys)
            logger.info(f"Cached typos: {len(new_keys)} new")
            return
        except Exception:
            pass
    logger.info("Fetching typo data from Datamuse...")
    try:
        fetched = {}
        for typo in list(FALLBACK_TYPOS.keys())[:50]:
            url = f"https://api.datamuse.com/words?sp={urllib.parse.quote(typo)}&max=1"
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if data and data[0].get("word"):
                    s = data[0]["word"].lower()
                    if s != typo:
                        fetched[typo] = s
            except Exception:
                continue
        new_entries = {k: v for k, v in fetched.items() if k not in FALLBACK_TYPOS}
        if new_entries:
            common_typos.update(new_entries)
            TYPO_CACHE_FILE.write_text(
                json.dumps(new_entries, indent=0), encoding="utf-8",
            )
            logger.info(f"Fetched {len(new_entries)} new typo fixes")
    except Exception as e:
        logger.error(f"Typo fetch failed: {e}")


# ============================================================
#  SENSITIVE DATA
# ============================================================
SENSITIVE_PATTERNS = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CreditCard", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("API_Key_OpenAI", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("API_Key_GitHub", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("API_Key_AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private_Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("Password_Inline", re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*\S+", re.I)),
]

_seen_sensitive = set()
_SEEN_MAX = 500


def luhn_check(num_str):
    digits = [int(c) for c in num_str if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan_for_sensitive_data(text):
    if len(text) < 8:
        return []
    hits = []
    for label, pattern in SENSITIVE_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0)
            if label == "CreditCard" and not luhn_check(value):
                continue
            key = (label, value[:12])
            if key in _seen_sensitive:
                continue
            if len(_seen_sensitive) >= _SEEN_MAX:
                _seen_sensitive.pop()
            _seen_sensitive.add(key)
            hits.append((label, value))
            logger.warning(f"[Sensitive] {label}: {value[:8]}...")
    return hits


# ============================================================
#  LoRA FINE-TUNE
# ============================================================
def collect_personal_corpus(text):
    try:
        with LORA_DATA_FILE.open("a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
    except Exception:
        pass


def run_lora_finetune():
    if not LORA_DATA_FILE.exists():
        logger.warning("No corpus for LoRA")
        return
    lines = [l for l in LORA_DATA_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) < 20:
        logger.warning(f"Only {len(lines)} lines; need 20+")
        return
    try:
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model, TaskType
        from transformers import (
            AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments,
        )
        logger.info(f"LoRA fine-tuning on {len(lines)} lines...")
        tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
        tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained("distilgpt2")
        lora_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM, r=8, lora_alpha=32,
            lora_dropout=0.1, target_modules=["c_attn"],
        )
        model = get_peft_model(model, lora_config)

        def tok(ex):
            return tokenizer(ex["text"], truncation=True, padding="max_length", max_length=128)

        ds = Dataset.from_dict({"text": lines}).map(tok, batched=True)
        ds = ds.map(lambda x: {"labels": x["input_ids"]}, batched=True)
        args = TrainingArguments(
            output_dir=str(LORA_ADAPTER_DIR), num_train_epochs=3,
            per_device_train_batch_size=2, gradient_accumulation_steps=4,
            learning_rate=2e-4, logging_steps=10, save_strategy="epoch",
            report_to="none", remove_unused_columns=False,
        )
        Trainer(model=model, args=args, train_dataset=ds).train()
        model.save_pretrained(str(LORA_ADAPTER_DIR))
        tokenizer.save_pretrained(str(LORA_ADAPTER_DIR))
        logger.info(f"LoRA adapter saved to {LORA_ADAPTER_DIR}")
    except Exception as e:
        logger.error(f"LoRA training failed: {e}")


# ============================================================
#  PUBLIC FILE SCAN
# ============================================================
SCANNABLE_EXTENSIONS = {".txt", ".md", ".csv"}
SCAN_DIRS = [
    pathlib.Path.home() / "Documents",
    pathlib.Path.home() / "Desktop",
    pathlib.Path.home() / "Downloads",
]


def scan_public_files(max_files=200, max_size_mb=2):
    if NAME_WHITELIST_FILE.exists():
        logger.info("File scan already done.")
        return
    logger.info("Scanning user files for vocabulary...")
    word_counter = Counter()
    scanned = 0

    def _iter_files():
        for base in SCAN_DIRS:
            if not base.exists():
                continue
            for ext in SCANNABLE_EXTENSIONS:
                for fp in base.rglob(f"*{ext}"):
                    yield fp

    for fp in _iter_files():
        if scanned >= max_files:
            break
        try:
            if fp.stat().st_size > max_size_mb * 1024 * 1024:
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if not text or not any(c.isalpha() for c in text[:500]):
                continue
            words = re.findall(r"[A-Za-z][A-Za-z']{2,}", text)
            word_counter.update(w.lower() for w in words)
            with LORA_DATA_FILE.open("a", encoding="utf-8") as f:
                for s in re.split(r"[.!?]\s+", text)[:200]:
                    s = s.strip()
                    if 20 <= len(s) <= 200:
                        f.write(s + "\n")
            scanned += 1
        except Exception:
            continue

    candidates = {
        w for w, c in word_counter.items()
        if c >= 5 and len(w) >= 3 and not _is_known_word(w)
    }
    if candidates:
        with NAME_WHITELIST_FILE.open("w", encoding="utf-8") as f:
            for w in sorted(candidates):
                f.write(w + "\n")
        name_whitelist.update(candidates)
        logger.info(f"Added {len(candidates)} domain words to whitelist from {scanned} files")
    else:
        logger.info(f"Scanned {scanned} files; no new whitelist words")


# ============================================================
#  CONFIG
# ============================================================
TOGGLE_AUTOCORRECT = "<ctrl>+<shift>+a"
TOGGLE_PREDICTION  = "<ctrl>+<shift>+p"
ACCEPT_PREDICTION  = "<ctrl>+<space>"
GRAMMAR_CHECK      = "<ctrl>+<shift>+g"
SHOW_HELP          = "<ctrl>+<shift>+h"
LORA_TRIGGER       = "<ctrl>+<shift>+l"

# Undo is handled inline in on_press (see UNDO_KEY_CHAR).
# pynput's GlobalHotKeys cannot match shifted punctuation keys,
# so we listen for the resulting character '~' directly.
UNDO_KEY_CHAR = "~"

TASK_NAME = "GluttonousTypr"

MIN_WORD_LENGTH = 3
MAX_EDIT_DISTANCE = 2
CORRECT_CAPITALIZED = True
CONTEXT_WINDOW = 3
PREDICTION_TOP_K = 5
PREDICTION_CONTEXT = 8
UNDO_TIMEOUT = 5.0
CONFIDENCE_GATE = 0.5
MULTIWORD_LEN = 3
PASTE_SKIP_WORDS = 3

SETTLE_BEFORE_DELETE = 0.025
BACKSPACE_DELAY = 0.003
TYPE_DELAY = 0.003

SKIP_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"\S+@\S+\.\S+"),
    re.compile(r"[a-zA-Z]:\\\S+"),
]

BOUNDARY_CHARS = {" ", "\t", "\n", ".", ",", "!", "?", ";", ":"}
BOUNDARY_KEYS = {
    keyboard.Key.space: " ",
    keyboard.Key.enter: "\n",
    keyboard.Key.tab:   "\t",
}

APP_HINTS = {
    "mail": "Subject: Re: ", "outlook": "Subject: Re: ", "gmail": "Subject: Re: ",
    "teams": "Hi, ", "slack": "Hi, ", "discord": "Hi, ",
    "word": "Dear ", "docs": "Dear ", "notepad": "", "code": "// ",
}

# Title-based exclusion (legacy — kept for backward compat)
EXCLUDED_APPS = ["Code", "Terminal", "PowerShell", "cmd", "vim", "notepad++"]

# Process-name exclusion — checked before any correction or prediction
EXCLUDED_PROCESSES = {
    # Code editors / IDEs
    "code.exe", "code-insiders.exe", "sublime_text.exe", "atom.exe",
    "pycharm64.exe", "pycharm.exe", "idea64.exe", "idea.exe",
    "webstorm64.exe", "devenv.exe", "notepad++.exe",
    "gvim.exe", "vim.exe", "emacs.exe", "nano.exe",
    # Terminals / shells
    "windowsterminal.exe", "wt.exe", "powershell.exe", "pwsh.exe",
    "cmd.exe", "conhost.exe", "mintty.exe", "putty.exe", "mobaxterm.exe",
    "wsl.exe", "bash.exe",
    # Games / launchers
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "epicgameslauncher.exe", "battle.net.exe", "agent.exe",
    "origin.exe", "eaapp.exe", "uplay.exe", "ubisoftconnect.exe",
    "riotclientservices.exe", "leagueclient.exe", "leagueclientux.exe",
    "valorant.exe", "csgo.exe", "cs2.exe", "dota2.exe",
    "minecraft.exe", "javaw.exe", "fortnite.exe", "r5apex.exe",
    "overwatch.exe", "destiny2.exe", "haloinfinite.exe",
    "robloxplayerbeta.exe", "robloxplayer.exe",
    "eldenring.exe", "gta5.exe", "rdr2.exe", "cyberpunk2077.exe",
    "witcher3.exe", "skyrimse.exe", "fallout4.exe",
    # Password managers
    "keepass.exe", "keepassxc.exe", "lastpass.exe", "bitwarden.exe",
    "1password.exe", "dashlane.exe", "keeper.exe",
    # Crypto wallets
    "electrum.exe", "exodus.exe", "ledgerlive.exe",
    # Sensitive system tools
    "regedit.exe", "mmc.exe", "taskmgr.exe",
}

# Title keyword exclusion
EXCLUDED_TITLE_KEYWORDS = {
    "search", "find", "address bar", "url bar", "omnibox",
    "regedit", "registry editor",
    "password", "sign in", "sign-in", "login", "log in", "log-in",
    "authenticate", "2fa", "verification code",
}

# Focused-control class name exclusion
EXCLUDED_CONTROL_CLASSES = {
    "chrome_omniboxview",
    "mozilla_omnibox",
    "searchboxedit",
    "searchbox",
    "windows.ui.core.corewindow",
    "richedit50w",
}


# ============================================================
#  STATE
# ============================================================
enabled_autocorrect = True
enabled_prediction = True

buffer = []
context_words = deque(maxlen=CONTEXT_WINDOW)
at_sentence_start = True

buffer_lock = threading.Lock()
state_lock = threading.Lock()
ignore_lock = threading.Lock()

ignore_count = 0
pending_prediction = None
pending_multiword = ""
last_correction = None
paste_words_remaining = 0
_pressed_modifiers = set()
_prediction_token = 0

ctrl = keyboard.Controller()
listener = None
tray_icon = None
_hotkeys = None


# ============================================================
#  HELPERS
# ============================================================
def is_safe_to_correct(word):
    for p in SKIP_PATTERNS:
        if p.search(word):
            return False
    if word.isdigit():
        return False
    if not any(c.isalpha() for c in word):
        return False
    try:
        word.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _preserve_case(original, corrected):
    if not CORRECT_CAPITALIZED:
        return corrected
    if original.isupper() and len(original) > 1:
        return corrected.upper()
    if original[0].isupper():
        return corrected.capitalize()
    return corrected


def _get_app_context():
    if not _HAS_GETWINDOW:
        return ""
    try:
        win = gw.getActiveWindow()
        return (win.title or "").lower() if win else ""
    except Exception:
        return ""


def _current_app_key():
    title = _get_app_context()
    for key in list(APP_HINTS.keys()) + ["chrome", "firefox", "edge"]:
        if key in title:
            return key
    return "default"


# ============================================================
#  CONTEXT EXCLUSION (password / search / games / IDEs)
# ============================================================
_exclusion_cache = {"time": 0.0, "value": False}


def should_skip_correction():
    """
    Return True if the foreground context is off-limits for corrections.
    Cached 500 ms to keep the hot path fast.
    """
    now = time.time()
    if now - _exclusion_cache["time"] < 0.5:
        return _exclusion_cache["value"]

    result = False
    try:
        # 1. Password field (fastest check, cached internally)
        if is_password_field():
            result = True

        # 2. Excluded process
        if not result:
            proc = get_active_process_name()
            if proc and proc in EXCLUDED_PROCESSES:
                logger.debug(f"[Skip] excluded process: {proc}")
                result = True

        # 3. Excluded control class (search bars)
        if not result:
            cls = get_focused_control_class().lower()
            if cls and cls in EXCLUDED_CONTROL_CLASSES:
                logger.debug(f"[Skip] excluded control class: {cls}")
                result = True

        # 4. Title keyword
        if not result:
            title = _get_app_context()
            if title:
                for kw in EXCLUDED_TITLE_KEYWORDS:
                    if kw in title:
                        logger.debug(f"[Skip] title keyword '{kw}'")
                        result = True
                        break

        # 5. Fullscreen non-browser app (likely a game)
        if not result and is_fullscreen_window():
            proc = get_active_process_name()
            browser_procs = {
                "chrome.exe", "msedge.exe", "firefox.exe",
                "brave.exe", "opera.exe", "vivaldi.exe",
            }
            if proc and proc not in browser_procs:
                logger.debug(f"[Skip] fullscreen app: {proc}")
                result = True

        # 6. Legacy title-based exclusion
        if not result:
            title = _get_app_context()
            if any(app.lower() in title for app in EXCLUDED_APPS):
                logger.debug(f"[Skip] legacy title match")
                result = True

    except Exception as e:
        logger.debug(f"should_skip_correction error: {e}")

    _exclusion_cache["time"] = now
    _exclusion_cache["value"] = result
    return result


# ============================================================
#  SPECIAL CASE
# ============================================================
def _special_case(word):
    if word == "i":
        return "I"
    if word == "a" and at_sentence_start:
        return "A"
    low = word.lower()
    if low in _SPECIAL_CONTRACTIONS:
        return _SPECIAL_CONTRACTIONS[low]
    return None


# ============================================================
#  CORRECTION
# ============================================================
def get_contextual_correction(word):
    if len(word) < MIN_WORD_LENGTH:
        return None
    if not is_safe_to_correct(word):
        return None

    target_lower = word.lower()

    if target_lower in SLANG_WHITELIST:
        return None
    if target_lower in SLANG_REPLACEMENTS:
        return _preserve_case(word, SLANG_REPLACEMENTS[target_lower])
    if target_lower in name_whitelist:
        return None
    if target_lower in KEYBOARD_ADJACENT:
        return _preserve_case(word, KEYBOARD_ADJACENT[target_lower])
    if target_lower in common_typos:
        return _preserve_case(word, common_typos[target_lower])

    with state_lock:
        ctx = list(context_words)[-(CONTEXT_WINDOW - 1):] if CONTEXT_WINDOW > 1 else []
    if ctx:
        ctx_key = f"{ctx[-1]}|{target_lower}"
        if wrong_corrections.get(ctx_key, 0) >= 2:
            return None

    if ENABLE_HOMOPHONE and _HOMOPHONE_INDEX.get(target_lower) and len(target_lower) >= 4:
        h = disambiguate_homophone(word, " ".join(ctx))
        if h and h != target_lower:
            return _preserve_case(word, h)

    phrase = " ".join(ctx + [word]) if ctx else word
    try:
        sug = sym_spell.lookup_compound(
            phrase, max_edit_distance=MAX_EDIT_DISTANCE,
            transfer_casing=True, ignore_non_words=True,
        )
    except Exception:
        sug = []
    if sug:
        tokens = sug[0].term.split()
        if tokens:
            corrected = tokens[-1]
            if len(corrected) <= len(word) * 2 + 2:
                if corrected.lower() != target_lower:
                    return _preserve_case(word, corrected)
                return None

    sug = sym_spell.lookup(
        target_lower, Verbosity.CLOSEST,
        max_edit_distance=MAX_EDIT_DISTANCE, include_unknown=False,
    )
    if not sug:
        return None
    corrected = sug[0].term
    if corrected == target_lower:
        return None
    return _preserve_case(word, corrected)


# ============================================================
#  KEY SIMULATION
# ============================================================
_MODIFIER_KEYS = (
    keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
    keyboard.Key.alt_l, keyboard.Key.alt_r,
    keyboard.Key.shift_l, keyboard.Key.shift_r,
)


def release_all_modifiers():
    for m in _MODIFIER_KEYS:
        try:
            ctrl.release(m)
        except Exception:
            pass


def _tap(key):
    ctrl.press(key)
    ctrl.release(key)


def type_string(text):
    for ch in text:
        if ch == "\n":
            _tap(keyboard.Key.enter)
        elif ch == "\t":
            _tap(keyboard.Key.tab)
        else:
            ctrl.type(ch)
        time.sleep(TYPE_DELAY)


def type_string_safe(text):
    release_all_modifiers()
    time.sleep(0.02)
    type_string(text)


def replace_word(original, boundary, corrected):
    global ignore_count
    n_back = len(original) + len(boundary)
    n_type = len(corrected) + len(boundary)
    with ignore_lock:
        ignore_count += n_back + n_type
    time.sleep(SETTLE_BEFORE_DELETE)
    for _ in range(n_back):
        _tap(keyboard.Key.backspace)
        time.sleep(BACKSPACE_DELAY)
    type_string(corrected + boundary)
    time.sleep(0.02)


# ============================================================
#  DELAYED UNDO (off the listener thread)
# ============================================================
def _delayed_undo():
    """
    Runs when the user presses Shift+`. Flow:
      1. Wait briefly so the user's physical Shift can be released.
      2. Release all simulated modifiers.
      3. Backspace the '~' that already reached the target app.
      4. Perform the standard undo.
    """
    global ignore_count
    time.sleep(0.10)
    release_all_modifiers()
    time.sleep(0.05)

    # Remove the '~' character that was just typed
    with ignore_lock:
        ignore_count += 1
    _tap(keyboard.Key.backspace)
    time.sleep(0.05)

    undo_last_correction()


# ============================================================
#  WORD PIPELINE
# ============================================================
def process_completed_word(word, boundary):
    global pending_prediction, pending_multiword, last_correction
    global at_sentence_start, paste_words_remaining, _prediction_token

    if paste_words_remaining > 0:
        paste_words_remaining -= 1
        if word:
            with state_lock:
                context_words.append(word.lower())
        return

    final_word = word
    skip = should_skip_correction()

    if enabled_autocorrect and not skip:
        special = _special_case(word)
        if special and special != word:
            replace_word(word, boundary, special)
            last_correction = (word, special, boundary, time.time())
            final_word = special
        else:
            correction = get_contextual_correction(word)
            if correction and correction != word:
                logger.debug(f"[Autocorrect] '{word}' -> '{correction}'")
                replace_word(word, boundary, correction)
                last_correction = (word, correction, boundary, time.time())
                personal_freq[correction.lower()] += 1
                final_word = correction
                scan_for_sensitive_data(correction)
            elif word:
                personal_freq[word.lower()] += 1
                scan_for_sensitive_data(word)

    is_sentence_end = boundary in ".!?\n"
    with state_lock:
        pending_prediction = None
        pending_multiword = ""
        if final_word:
            context_words.append(final_word.lower())
        at_sentence_start = is_sentence_end
        _prediction_token += 1

    app_key = _current_app_key()
    if final_word:
        if app_key not in app_contexts:
            app_contexts[app_key] = {"words": Counter(), "snippets": []}
        app_contexts[app_key]["words"][final_word.lower()] += 1
        if boundary in ".!?":
            with state_lock:
                app_contexts[app_key]["snippets"].append(" ".join(list(context_words)))

    if not skip:
        show_prediction()


def dispatch(word, boundary):
    threading.Thread(
        target=process_completed_word, args=(word, boundary), daemon=True,
    ).start()


def show_prediction():
    global pending_prediction, pending_multiword
    if not enabled_prediction:
        return
    if should_skip_correction():
        return
    with state_lock:
        token = _prediction_token

    preds = predict_next_words()
    if not preds:
        return
    preds.sort(key=lambda w: -personal_freq.get(w, 0))

    with state_lock:
        if _prediction_token != token:
            return
        pending_prediction = preds[0]
    logger.debug(f"[Prediction] {', '.join(preds[:3])}")

    with state_lock:
        ctx_text = " ".join(list(context_words)[-PREDICTION_CONTEXT:])
    if not ctx_text:
        return

    def _mw():
        global pending_multiword
        mw = predict_multiword(ctx_text)
        if mw and len(mw.split()) >= 2:
            with state_lock:
                if _prediction_token != token:
                    return
                pending_multiword = mw
            logger.debug(f"[Multi-word] '{mw}'")

    threading.Thread(target=_mw, daemon=True).start()


# ============================================================
#  KEY HANDLER
# ============================================================
def on_press(key):
    global ignore_count, paste_words_remaining

    if key in _MODIFIER_KEYS:
        _pressed_modifiers.add(key)

    ctrl_held = (keyboard.Key.ctrl_l in _pressed_modifiers or
                 keyboard.Key.ctrl_r in _pressed_modifiers)
    if ctrl_held:
        try:
            if key.char in ("v", "V", "\x16"):
                paste_words_remaining = PASTE_SKIP_WORDS
                with state_lock:
                    context_words.clear()
                logger.debug(f"Paste detected; skipping next {PASTE_SKIP_WORDS} words")
        except AttributeError:
            pass

    with ignore_lock:
        if ignore_count > 0:
            ignore_count -= 1
            return

    # --- Undo hotkey: Shift+` produces '~' ---
    try:
        ch = key.char
    except AttributeError:
        ch = None

    if ch == UNDO_KEY_CHAR:
        lc = last_correction
        if lc and (time.time() - lc[3]) < UNDO_TIMEOUT:
            logger.debug("[Undo] Shift+` detected; scheduling undo")
            threading.Thread(target=_delayed_undo, daemon=True).start()
            return

    if not enabled_autocorrect and not enabled_prediction:
        return

    if key == keyboard.Key.backspace:
        if buffer:
            buffer.pop()
        return

    if key == keyboard.Key.esc:
        buffer.clear()
        with state_lock:
            pending_prediction = None
            pending_multiword = ""
        return

    if ch is not None:
        if ch in BOUNDARY_CHARS:
            if buffer:
                word = "".join(buffer)
                buffer.clear()
                dispatch(word, ch)
            return
        buffer.append(ch)
        return

    if key in BOUNDARY_KEYS:
        b = BOUNDARY_KEYS[key]
        if buffer:
            word = "".join(buffer)
            buffer.clear()
            dispatch(word, b)
        return


def on_release(key):
    _pressed_modifiers.discard(key)


# ============================================================
#  HOTKEY HANDLERS
# ============================================================
def toggle_autocorrect():
    global enabled_autocorrect
    enabled_autocorrect = not enabled_autocorrect
    with buffer_lock:
        buffer.clear()
    logger.info(f"[Autocorrect] {'ON' if enabled_autocorrect else 'OFF'}")
    _refresh_tray()


def toggle_prediction():
    global enabled_prediction, pending_prediction, pending_multiword
    enabled_prediction = not enabled_prediction
    with state_lock:
        pending_prediction = None
        pending_multiword = ""
    logger.info(f"[Prediction] {'ON' if enabled_prediction else 'OFF'}")
    _refresh_tray()


def accept_prediction():
    global pending_prediction, pending_multiword, ignore_count
    with state_lock:
        mw = pending_multiword
        word = pending_prediction
        pending_prediction = None
        pending_multiword = ""

    text = mw if mw else (word if word else "")
    if not text:
        return

    with ignore_lock:
        ignore_count += len(text) + 1
    type_string_safe(text + " ")
    logger.info(f"[Prediction] accepted: '{text}'")

    for w in text.split():
        w = w.strip().lower()
        if w:
            with state_lock:
                context_words.append(w)
    collect_personal_corpus(text)


def undo_last_correction():
    global last_correction, ignore_count
    if not last_correction:
        logger.info("[Undo] Nothing to undo.")
        return
    original, corrected, boundary, ts = last_correction
    if time.time() - ts > UNDO_TIMEOUT:
        logger.info("[Undo] Timeout.")
        last_correction = None
        return
    logger.info(f"[Undo] '{corrected}' -> '{original}'")
    with state_lock:
        ctx = list(context_words)[-1] if context_words else ""
    wrong_corrections[f"{ctx}|{original.lower()}"] += 1

    release_all_modifiers()
    time.sleep(0.03)

    n_back = len(corrected) + len(boundary)
    n_type = len(original) + len(boundary)
    with ignore_lock:
        ignore_count += n_back + n_type

    time.sleep(SETTLE_BEFORE_DELETE)
    for _ in range(n_back):
        _tap(keyboard.Key.backspace)
        time.sleep(BACKSPACE_DELAY)
    type_string(original + boundary)
    last_correction = None


_grammar_tool = None
_grammar_lock = threading.Lock()


def get_grammar_tool():
    global _grammar_tool
    with _grammar_lock:
        if _grammar_tool is None:
            try:
                import language_tool_python
                logger.info("Loading LanguageTool (Java)...")
                _grammar_tool = language_tool_python.LanguageTool("en-US")
                logger.info("LanguageTool ready.")
            except Exception as e:
                logger.error(f"LanguageTool unavailable: {e}")
                return None
        return _grammar_tool


def run_grammar_check():
    tool = get_grammar_tool()
    if tool is None:
        return
    with state_lock:
        text = " ".join(list(context_words)[-20:])
        if buffer:
            text += " " + "".join(buffer)
    if not text.strip():
        return
    matches = tool.check(text)
    if not matches:
        logger.info("[Grammar] No issues.")
        return
    for m in matches[:5]:
        logger.info(f"[Grammar] {m.message}")


def grammar_check():
    threading.Thread(target=run_grammar_check, daemon=True).start()


def show_help():
    text = (
        "Ctrl+Shift+A  toggle autocorrect\n"
        "Ctrl+Shift+P  toggle prediction\n"
        "Ctrl+Space    accept prediction\n"
        "Shift+`       undo last correction (within 5 s)\n"
        "Ctrl+Shift+G  grammar check\n"
        "Ctrl+Shift+L  LoRA fine-tune"
    )
    logger.info("Hotkeys:\n" + text)
    if tray_icon and _HAS_TRAY:
        try:
            tray_icon.notify(text, "GluttonousTypr Hotkeys")
        except Exception:
            pass


def trigger_lora():
    threading.Thread(target=run_lora_finetune, daemon=True).start()


# ============================================================
#  START-AT-LOGON
# ============================================================
def _task_exists():
    try:
        import win32com.client
        s = win32com.client.Dispatch("Schedule.Service")
        s.Connect()
        s.GetFolder("\\").GetTask(TASK_NAME)
        return True
    except Exception:
        return False


def _resolve_pythonw():
    import shutil as _sh
    candidate = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    candidate = pathlib.Path(sys.executable).parent / "pythonw.exe"
    if candidate.exists():
        return str(candidate)
    which = _sh.which("pythonw")
    if which:
        return which
    logger.warning("pythonw.exe not found; using python.exe with hidden console")
    return sys.executable


def enable_start_at_logon():
    try:
        import win32com.client
        s = win32com.client.Dispatch("Schedule.Service")
        s.Connect()
        root = s.GetFolder("\\")
        td = s.NewTask(0)
        td.RegistrationInfo.Description = "GluttonousTypr"
        td.Principal.RunLevel = 0
        td.Principal.LogonType = 3
        td.Settings.Enabled = True
        td.Settings.ExecutionTimeLimit = "PT0S"
        td.Settings.DisallowStartIfOnBatteries = False
        td.Settings.StopIfGoingOnBatteries = False
        td.Settings.Hidden = False
        td.Settings.AllowDemandStart = True
        tr = td.Triggers.Create(9)
        tr.UserId = os.environ.get("USERNAME", "")
        exe = _resolve_pythonw()
        a = td.Actions.Create(0)
        a.Path = exe
        a.Arguments = f'"{os.path.abspath(__file__)}"'
        a.WorkingDirectory = str(pathlib.Path(__file__).parent)
        root.RegisterTaskDefinition(TASK_NAME, td, 6, None, None, 3)
        logger.info(f"Start at logon ENABLED (exe={exe}).")
        return True
    except Exception as e:
        logger.error(f"Enable start-at-logon failed: {e}")
        return False


def disable_start_at_logon():
    try:
        import win32com.client
        s = win32com.client.Dispatch("Schedule.Service")
        s.Connect()
        s.GetFolder("\\").DeleteTask(TASK_NAME, 0)
        logger.info("Start at logon DISABLED.")
        return True
    except Exception as e:
        logger.error(f"Disable failed: {e}")
        return False


def toggle_start_at_logon(icon, item):
    if _task_exists():
        disable_start_at_logon()
    else:
        enable_start_at_logon()
    _refresh_tray()


# ============================================================
#  TRAY
# ============================================================
def _make_icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color)
    d.text((22, 14), "a", fill="white")
    return img


def _tray_menu():
    return pystray.Menu(
        pystray.MenuItem("Autocorrect", lambda i, it: toggle_autocorrect(),
                         checked=lambda it: enabled_autocorrect),
        pystray.MenuItem("Prediction", lambda i, it: toggle_prediction(),
                         checked=lambda it: enabled_prediction),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at logon", toggle_start_at_logon,
                         checked=lambda it: _task_exists()),
        pystray.MenuItem("Fine-tune (LoRA)", lambda i, it: trigger_lora()),
        pystray.MenuItem("Show hotkeys", lambda i, it: show_help()),
        pystray.MenuItem("Open log", lambda i, it: _open_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda i, it: _quit_from_tray(i)),
    )


def _open_log():
    try:
        os.startfile(str(LOG_FILE))
    except Exception as e:
        logger.error(f"Open log failed: {e}")


def _quit_from_tray(icon):
    icon.stop()
    shutdown()


def _refresh_tray():
    if tray_icon and _HAS_TRAY:
        try:
            tray_icon.update_menu()
        except Exception:
            pass


def _tray_thread():
    global tray_icon
    if not _HAS_TRAY:
        return
    color = (60, 180, 75) if (enabled_autocorrect and enabled_prediction) else (200, 60, 60)
    tray_icon = pystray.Icon(
        "gluttonoustypr", _make_icon_image(color),
        "GluttonousTypr v9.6", menu=_tray_menu(),
    )
    tray_icon.run()


# ============================================================
#  SHUTDOWN
# ============================================================
_shutdown_done = threading.Event()


def shutdown(*_args):
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    logger.info("Shutting down...")
    _pressed_modifiers.clear()
    time.sleep(0.15)
    save_personal_data()
    if listener:
        try:
            listener.stop()
        except Exception:
            pass
    if _hotkeys:
        try:
            _hotkeys.stop()
        except Exception:
            pass
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    logging.shutdown()


atexit.register(shutdown)


def _signal_handler(signum, frame):
    logger.info(f"Signal {signum}")
    shutdown()
    sys.exit(0)


def _periodic_save():
    while not _shutdown_done.is_set():
        time.sleep(30)
        if _shutdown_done.is_set():
            break
        save_personal_data()


# ============================================================
#  MAIN
# ============================================================
def main():
    global listener, _hotkeys

    hide_console_window()

    logger.info("=" * 60)
    logger.info("  GluttonousTypr v9.6")
    logger.info("=" * 60)
    logger.info("  Ctrl+Shift+A  autocorrect")
    logger.info("  Ctrl+Shift+P  prediction")
    logger.info("  Ctrl+Space    accept prediction")
    logger.info("  Shift+`       undo")
    logger.info("  Ctrl+Shift+G  grammar")
    logger.info("=" * 60)
    logger.info(f"  Typos: {len(FALLBACK_TYPOS)} | "
                f"Adjacent: {len(KEYBOARD_ADJACENT)} | "
                f"Homophones: {len(HOMOPHONE_GROUPS)} | "
                f"Confused: {len(CONFUSED_WORDS)} | "
                f"Slang: {len(SLANG_WHITELIST)} | "
                f"Excl. processes: {len(EXCLUDED_PROCESSES)}")

    def _cleanup_old_task():
        try:
            import win32com.client
            s = win32com.client.Dispatch("Schedule.Service")
            s.Connect()
            s.GetFolder("\\").DeleteTask("GlobalAutocorrect", 0)
            logger.info("Removed old task 'GlobalAutocorrect'")
        except Exception:
            pass

    threading.Thread(target=_cleanup_old_task, daemon=True).start()

    load_personal_data()
    _start_model_loading()
    threading.Thread(target=fetch_common_typos, daemon=True).start()
    threading.Thread(target=scan_public_files, daemon=True).start()
    if _HAS_TRAY:
        threading.Thread(target=_tray_thread, daemon=True).start()
    else:
        logger.warning("pystray not installed; tray icon disabled")
    threading.Thread(target=_periodic_save, daemon=True).start()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        pass

    hotkeys_map = {
        TOGGLE_AUTOCORRECT: toggle_autocorrect,
        TOGGLE_PREDICTION:  toggle_prediction,
        ACCEPT_PREDICTION:  accept_prediction,
        # UNDO handled inline in on_press (Shift+` → '~')
        GRAMMAR_CHECK:      grammar_check,
        SHOW_HELP:          show_help,
        LORA_TRIGGER:       trigger_lora,
    }
    _hotkeys = keyboard.GlobalHotKeys(hotkeys_map)
    _hotkeys.start()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()
