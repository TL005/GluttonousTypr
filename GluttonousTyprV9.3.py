"""
Global Autocorrect + Deep Learning Prediction — v9.4
Requires: pip install pynput symspellpy transformers torch pystray pillow
          pygetwindow language-tool-python pywin32 onnxruntime
          "optimum[onnxruntime]" peft datasets

Launch:
  pythonw.exe global_autocorrect.py    silent background
  pyw global_autocorrect.py            silent (py launcher)
  python global_autocorrect.py         visible for debugging (auto-hides)

Hotkeys:
  Ctrl+Shift+A   toggle autocorrect
  Ctrl+Shift+P   toggle prediction
  Ctrl+Space     accept prediction
  Ctrl+Alt+Z     undo last correction
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
APP_DIR = pathlib.Path.home() / ".autocorrect"
APP_DIR.mkdir(exist_ok=True)

LOG_FILE               = APP_DIR / "autocorrect.log"
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

logger = logging.getLogger("autocorrect")
logger.setLevel(logging.DEBUG)
logger.addHandler(_handler)

if _HAS_CONSOLE:
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_console_handler)

logger.info("=" * 50)
logger.info("Autocorrect v9.4 starting")
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

ES_PASSWORD = 0x0020
GWL_STYLE = -16


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
#  LOOKUP TABLE: KEYBOARD_ADJACENT
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


# ============================================================
#  LOOKUP TABLE: FALLBACK_TYPOS
# ============================================================
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
    "allmost": "almost", "alot": "a lot", "amatuer": "amateur",
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


# ============================================================
#  LOOKUP TABLE: CONFUSED_WORDS
# ============================================================
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


# ============================================================
#  LOOKUP TABLE: SLANG_WHITELIST
#  Never corrected, even if SymSpell flags them.
# ============================================================
SLANG_WHITELIST = {
    # --- Greetings / address ---
    "yo", "yoyo", "sup", "wassup", "wazzup", "whassup", "whatup", "waddup",
    "hey", "heya", "heyy", "heyyy", "hii", "hiii", "yooo", "yoooo",
    "homie", "homey", "homies", "bro", "broski", "bruh", "bruhh", "brah",
    "brudda", "bredren", "bredrin", "fam", "famalam", "cuz", "cuzzo",
    "dawg", "dawgs", "g", "gee", "playa", "player", "pimp", "boss",
    "chief", "king", "queen", "sis", "sista", "brotha", "brutha", "sistah",
    "gurl", "girl", "boo", "bae", "babe", "baby", "shawty", "shorty",
    "shordy", "shawtys", "shorties", "mami", "papi", "mijo", "mija",
    "homes", "homeslice", "homiette", "peeps", "peepz", "folks", "folkz",

    # --- Reactions / interjections ---
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

    # --- Approval / praise ---
    "dope", "fire", "lit", "litt", "litty", "bussin",
    "slaps", "slappin", "bangs", "bangin", "crackin",
    "tight", "sick", "wicked", "ill", "killer", "killin", "kilt",
    "raw", "hard", "clutch", "goated", "goat", "goted",
    "mid", "mids", "trash", "garbage", "whack", "bunk",
    "snatched", "slay", "slayy", "slayed", "slaying",
    "periodt", "period", "yass", "yasss", "queen", "werk",

    # --- Disapproval / insults ---
    "sus", "sussy", "cringe", "cringey", "cringy",
    "basic", "thirsty", "thot", "thottie", "hoe", "hoes",
    "clown", "clownin", "buffoon", "bozo", "dingus", "dingbat", "dodo",
    "dummy", "dumdum", "numbskull", "numpty", "knucklehead", "blockhead",
    "sucker", "punk", "chump", "jerk", "jerkface", "jackass",
    "jabroni", "scrub", "bum", "bums", "hater", "haters", "hatin",
    "fuckboy", "fboi", "fuccboi", "simp", "simps", "simpy", "simping",
    "incel", "incels", "karen", "karens", "chad", "chads",
    "neckbeard", "neckbeards", "noob", "newb", "newbie", "n00b",

    # --- People / relationships ---
    "booboo", "babygirl", "babyboy", "wifey", "hubby", "main", "mainthing",
    "sidepiece", "sidechick", "hookup", "hookups",
    "roast", "roasted", "roasting", "dissing", "dis", "disses",
    "beefing", "beefin", "beef", "beefs", "shade", "shady", "petty",
    "messy", "messiness", "drama", "dramatic", "dramaqueen",
    "squad", "squads", "crew", "crews", "posse", "clique", "gang",
    "tribe", "tribes", "squadup", "squaddeep", "squadgoals",
    "rideordie", "bestie", "besties", "bff", "bffs",
    "roomie", "roomies", "broham", "brohams",

    # --- Actions / verbs ---
    "chill", "chillax", "chillin", "chillen", "vibin", "vibing",
    "vibe", "vibes", "vibey", "goodvibes", "badb vibes".replace(" ", ""),
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

    # --- Money ---
    "cheddar", "paper", "papers", "bands", "bandz", "racks", "rackz",
    "guap", "gwap", "moolah", "moola", "scratch", "bread", "dough",
    "coin", "coins", "cashflow", "bigmoney", "bag", "bags",
    "broke", "brokeboi", "brokeboy", "rich", "wealthy", "moneyed",

    # --- Weed culture ---
    "dank", "danks", "gas", "za", "zaza", "pack", "packs",
    "loud", "loudpack", "tree", "trees", "herb", "herbs", "bud", "buds",
    "green", "greens", "exotic", "exotics",
    "stoned", "blazed", "faded", "fried", "toasted",
    "roasted", "baked", "cooked", "gassed", "zoned", "zonedout",

    # --- Music / culture ---
    "trap", "trapping", "drill", "drilling", "drip", "dripping", "drippy",
    "sauce", "saucy", "beat", "beats", "flow",
    "bars", "punchline", "punchlines", "freestyle", "cypher", "cyphers",
    "mixtape", "mixtapes", "hook", "hooks", "verse", "verses",
    "disstrack", "clapback", "clapbacks",
    "spitting", "spit", "spittin", "wildnout",

    # --- Internet / gaming ---
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

    # --- Regional US ---
    "yall", "yalls", "yins", "yinz", "youse", "yous",
    "aint", "gonna", "wanna", "gotta", "finna",
    "tryna", "shoulda", "coulda", "woulda", "musta", "hafta",
    "lemme", "gimme", "dunno", "dontcha", "didntcha",
    "cantcha", "wontcha", "fixin", "fixing", "yonder",
    "howdy", "howdies",

    # --- Filler / hedging ---
    "yanno", "yaknow", "yakno", "yuh", "yea", "yeah", "yep", "yup", "yupp",
    "yah", "naw", "nah", "blah", "anyways", "anywho", "anywayz",
    "whatever", "whatevs", "whatev",

    # --- AAVE specific ---
    "finsta", "chile", "chilee", "chyle", "gworl", "gworls",
    "sisses", "brothas", "sistas", "aight", "aiight", "igh", "ight",
    "dassit", "dasit", "datsit", "dass", "dat", "dese", "doe",
    "gwan", "trynna", "tryin", "bougie", "boujee", "bouj", "boujie",
    "ratchet", "ghetto", "ghettos", "chie", "honey", "hon",
    "hun", "hunn", "hunnies", "honeyy", "shug", "sugar",
    "lawdd", "lordt", "lordhamercy", "gawd", "gawwd", "gawdd",
    "gawt", "gawta", "gawtcha", "gon", "gone", "gonbe", "gonn",
    "ya", "yaa", "yall", "yalls",

    # --- Intensifiers / modifiers ---
    "hella", "helluva", "hecka",
    "lowkey", "highkey", "deadass", "realtalk",
    "tbh", "ngl", "imo", "imho", "fwiw", "btw", "brb", "afk",
    "irl", "tmi", "ttyl", "ttys", "hmu", "hitmeup",
    "dm", "dms", "dmed", "dming", "sliding", "slid", "slide",

    # --- Modern / TikTok ---
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


# ============================================================
#  LOOKUP TABLE: SLANG_REPLACEMENTS
#  Misspelled slang -> correct slang form.
# ============================================================
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
    "welpp": "welp", "mehh": "meh", "ughh": "ugh", "ughhh":
