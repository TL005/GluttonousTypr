"""
Global Autocorrect + Deep Learning Prediction — v7 (fully integrated)
Requires: pip install pynput symspellpy transformers torch pystray pillow
          pygetwindow language-tool-python pywin32
Run as:   python global_autocorrect.py

Hotkeys:
  Ctrl+Shift+A   toggle autocorrect
  Ctrl+Shift+P   toggle prediction
  Ctrl+Space     accept prediction (when shown)
  Ctrl+Shift+Z   undo last correction (within 5 seconds)
  Ctrl+Shift+G   grammar check current sentence
  Ctrl+Shift+H   show hotkey cheat sheet in tray notification
"""

# ============================================================
#  IMPORTS
# ============================================================
import atexit
import json
import logging
import logging.handlers
import os
import pathlib
import re
import signal
import sys
import threading
import time
import urllib.request
import importlib
import importlib.resources
from collections import Counter, deque
from datetime import datetime

try:
    keyboard = importlib.import_module("pynput.keyboard")
except ModuleNotFoundError as exc:
    if exc.name == "pynput":
        raise ModuleNotFoundError(
            "pynput is required; install it with 'python -m pip install pynput'"
        ) from exc
    raise
from symspellpy import SymSpell, Verbosity

# Optional GUI deps (imported lazily where possible)
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
#  PATHS AND LOGGING (feature 17)
# ============================================================
APP_DIR = pathlib.Path.home() / ".autocorrect"
APP_DIR.mkdir(exist_ok=True)

LOG_FILE = APP_DIR / "autocorrect.log"
PERSONAL_DICT_FILE = APP_DIR / "personal_dict.json"
TYPO_CACHE_FILE = APP_DIR / "common_typos.json"
WRONG_CORRECTIONS_FILE = APP_DIR / "wrong_corrections.json"

_log_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logger = logging.getLogger("autocorrect")
logger.setLevel(logging.DEBUG)
logger.addHandler(_log_handler)

# Also print to console (works when run interactively)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_console_handler)

logger.info("=" * 50)
logger.info("Autocorrect starting up")

# ============================================================
#  DEEP LEARNING MODEL (loads in background)
# ============================================================
print("[Setup] Loading PyTorch (CPU)...")
import torch
torch.set_num_threads(4)
from transformers import GPT2LMHeadModel, GPT2TokenizerFast

_model = None
_tokenizer = None
_model_ready = threading.Event()


def _load_dl_model():
    global _model, _tokenizer
    try:
        logger.info("Loading DistilGPT-2 (first run downloads ~330 MB)...")
        _tokenizer = GPT2TokenizerFast.from_pretrained("distilgpt2")
        _model = GPT2LMHeadModel.from_pretrained("distilgpt2")
        _model.eval()
        logger.info("DistilGPT-2 ready.")
    except Exception as e:
        logger.error(f"Deep learning unavailable: {e}")
    finally:
        _model_ready.set()


threading.Thread(target=_load_dl_model, daemon=True).start()

# ============================================================
#  SPELL CHECKER
# ============================================================
logger.info("Loading SymSpell dictionaries...")
sym_spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
_dict_path   = importlib.resources.files("symspellpy") / "frequency_dictionary_en_82_765.txt"
_bigram_path = importlib.resources.files("symspellpy") / "frequency_bigramdictionary_en_243_342.txt"
sym_spell.load_dictionary(str(_dict_path), term_index=0, count_index=1)
sym_spell.load_bigram_dictionary(str(_bigram_path), term_index=0, count_index=2)
logger.info(f"Dictionary loaded: {sym_spell.word_count:,} words")

# ============================================================
#  PERSISTENT PERSONAL DICTIONARY (feature 3)
# ============================================================
personal_freq = Counter()
wrong_corrections = Counter()   # (context_bigram, bad_word) -> count


def load_personal_data():
    global personal_freq, wrong_corrections
    if PERSONAL_DICT_FILE.exists():
        try:
            data = json.loads(PERSONAL_DICT_FILE.read_text(encoding="utf-8"))
            personal_freq.update(data)
            logger.info(f"Loaded {len(data)} personal words")
        except Exception as e:
            logger.error(f"Failed to load personal dict: {e}")
    if WRONG_CORRECTIONS_FILE.exists():
        try:
            data = json.loads(WRONG_CORRECTIONS_FILE.read_text(encoding="utf-8"))
            # Keys are strings; convert back to tuples
            for k, v in data.items():
                wrong_corrections[k] = v
            logger.info(f"Loaded {len(data)} wrong-correction records")
        except Exception as e:
            logger.error(f"Failed to load wrong corrections: {e}")


def save_personal_data():
    try:
        PERSONAL_DICT_FILE.write_text(
            json.dumps(dict(personal_freq), indent=0), encoding="utf-8",
        )
        WRONG_CORRECTIONS_FILE.write_text(
            json.dumps(dict(wrong_corrections), indent=0), encoding="utf-8",
        )
        logger.info("Personal data saved")
    except Exception as e:
        logger.error(f"Failed to save personal data: {e}")


# ============================================================
#  COMMON TYPOS FROM INTERNET (feature 4)
# ============================================================
# Hardcoded fallback (used if network is unavailable)
FALLBACK_TYPOS = {
    "teh": "the", "adn": "and", "taht": "that", "recieve": "receive",
    "seperate": "separate", "definately": "definitely", "occured": "occurred",
    "wich": "which", "thier": "their", "alot": "a lot",
    "beleive": "believe", "acheive": "achieve", "concious": "conscious",
    "enviroment": "environment", "goverment": "government", "occassion": "occasion",
    "neccessary": "necessary", "tommorow": "tomorrow", "untill": "until",
    "wierd": "weird", "freind": "friend", "buisness": "business",
}

common_typos = dict(FALLBACK_TYPOS)


def fetch_common_typos():
    """
    Fetch common misspellings from the Datamuse API and cache locally.
    Runs in a background thread on first launch.
    """
    global common_typos
    if TYPO_CACHE_FILE.exists():
        try:
            cached = json.loads(TYPO_CACHE_FILE.read_text(encoding="utf-8"))
            common_typos.update(cached)
            logger.info(f"Loaded {len(cached)} cached typo fixes")
            return
        except Exception as e:
            logger.error(f"Typo cache load failed: {e}")

    logger.info("Fetching common typos from Datamuse API...")
    try:
        # Datamuse endpoint: words that sound like the misspelling
        # We query a set of known common misspellings and pick the top
        # suggestion that is a valid word.
        seed_typos = list(FALLBACK_TYPOS.keys())
        fetched = {}
        for typo in seed_typos:
            url = f"https://api.datamuse.com/words?sp={urllib.parse.quote(typo)}&max=1"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data and data[0].get("word"):
                    suggestion = data[0]["word"].lower()
                    if suggestion != typo:
                        fetched[typo] = suggestion
            except Exception:
                continue   # skip individual failures

        if fetched:
            common_typos.update(fetched)
            TYPO_CACHE_FILE.write_text(
                json.dumps(fetched, indent=0), encoding="utf-8",
            )
            logger.info(f"Fetched {len(fetched)} typo fixes from Datamuse")
        else:
            logger.warning("Datamuse returned no results; using fallback")
    except Exception as e:
        logger.error(f"Typo fetch failed: {e}")


# ============================================================
#  GRAMMAR TOOL (feature 9)
# ============================================================
_grammar_tool = None
_grammar_lock = threading.Lock()


def get_grammar_tool():
    global _grammar_tool
    with _grammar_lock:
        if _grammar_tool is None:
            try:
                import language_tool_python
                logger.info("Initializing LanguageTool...")
                _grammar_tool = language_tool_python.LanguageTool("en-US")
                logger.info("LanguageTool ready.")
            except Exception as e:
                logger.error(f"LanguageTool unavailable: {e}")
                return None
        return _grammar_tool


def run_grammar_check():
    """On-demand grammar check of the current sentence buffer."""
    tool = get_grammar_tool()
    if tool is None:
        logger.warning("Grammar check requested but LanguageTool unavailable")
        return
    with state_lock:
        text = " ".join(list(context_words)[-20:])
        if buffer:
            text += " " + "".join(buffer)
    if not text.strip():
        return
    matches = tool.check(text)
    if not matches:
        logger.info("[Grammar] No issues found.")
        return
    logger.info(f"[Grammar] {len(matches)} issue(s):")
    for m in matches[:5]:
        logger.info(f"  - {m.message}  (offset {m.offset})")


# ============================================================
#  CONFIGURATION
# ============================================================
TOGGLE_AUTOCORRECT = "<ctrl>+<shift>+a"
TOGGLE_PREDICTION  = "<ctrl>+<shift>+p"
ACCEPT_PREDICTION  = "<ctrl>+<space>"
UNDO_CORRECTION    = "<ctrl>+<shift>+z"
GRAMMAR_CHECK      = "<ctrl>+<shift>+g"

TASK_NAME = "GlobalAutocorrect"

MIN_WORD_LENGTH    = 3
MAX_EDIT_DISTANCE  = 2
CORRECT_CAPITALIZED = True
CONTEXT_WINDOW     = 3
PREDICTION_TOP_K   = 5
PREDICTION_CONTEXT = 8
UNDO_TIMEOUT       = 5.0    # seconds
CONFIDENCE_GATE    = 0.5    # logit margin threshold (feature 13)

SETTLE_BEFORE_DELETE = 0.025
BACKSPACE_DELAY      = 0.003
TYPE_DELAY           = 0.003

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

# Application-type hints for context-aware prediction (feature 6)
APP_HINTS = {
    "mail":    "Subject: Re: ",
    "outlook": "Subject: Re: ",
    "gmail":   "Subject: Re: ",
    "teams":   "Hi, ",
    "slack":   "Hi, ",
    "discord": "Hi, ",
    "word":    "Dear ",
    "docs":    "Dear ",
    "notepad": "",
    "code":    "// ",
}

EXCLUDED_APPS = ["Code", "Terminal", "PowerShell", "cmd", "vim", "notepad++"]


# ============================================================
#  STATE
# ============================================================
enabled_autocorrect = True
enabled_prediction  = True

buffer = []
context_words = deque(maxlen=CONTEXT_WINDOW)

buffer_lock = threading.Lock()
state_lock  = threading.Lock()
ignore_lock = threading.Lock()

ignore_count = 0
pending_prediction = None
last_correction = None   # (original, corrected, boundary, timestamp)
personal_freq = Counter()   # re-declared for clarity; populated by load_personal_data

ctrl = keyboard.Controller()
listener = None
tray_icon = None


# ============================================================
#  SPELL CORRECTION (features 12)
# ============================================================
def is_safe_to_correct(word: str) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern.search(word):
            return False
    if word.isdigit():
        return False
    if not any(c.isalpha() for c in word):
        return False
    return True


def _preserve_case(original: str, corrected: str) -> str:
    if not CORRECT_CAPITALIZED:
        return corrected
    if original.isupper() and len(original) > 1:
        return corrected.upper()
    if original[0].isupper():
        return corrected.capitalize()
    return corrected


def _get_app_context() -> str:
    """Detect the active window title (feature 6)."""
    if not _HAS_GETWINDOW:
        return ""
    try:
        win = gw.getActiveWindow()
        return (win.title or "").lower() if win else ""
    except Exception:
        return ""


def _is_excluded_app() -> bool:
    title = _get_app_context()
    return any(app.lower() in title for app in EXCLUDED_APPS)


def get_contextual_correction(word: str):
    """Return corrected word or None. Uses hardcoded typos + SymSpell."""
    if len(word) < MIN_WORD_LENGTH:
        return None
    if not is_safe_to_correct(word):
        return None

    target_lower = word.lower()

    # (1) Hardcoded / fetched common typos (feature 4)
    if target_lower in common_typos:
        corrected = common_typos[target_lower]
        return _preserve_case(word, corrected)

    # (2) Learning from undos — skip corrections the user reverted (feature 11)
    with state_lock:
        ctx = list(context_words)[-(CONTEXT_WINDOW - 1):] if CONTEXT_WINDOW > 1 else []
    if ctx:
        ctx_key = f"{ctx[-1]}|{target_lower}"
        if wrong_corrections.get(ctx_key, 0) >= 2:
            logger.debug(f"Skipping '{word}' (previously undone)")
            return None

    # (3) Context-aware SymSpell lookup
    phrase = " ".join(ctx + [word]) if ctx else word

    try:
        suggestions = sym_spell.lookup_compound(
            phrase,
            max_edit_distance=MAX_EDIT_DISTANCE,
            transfer_casing=True,
            ignore_non_words=True,
        )
    except Exception:
        suggestions = []

    if suggestions:
        tokens = suggestions[0].term.split()
        if tokens:
            corrected = tokens[-1]
            if len(corrected) <= len(word) * 2 + 2:
                if corrected.lower() != target_lower:
                    # Frequency-aware ranking (feature 12)
                    if personal_freq.get(corrected.lower(), 0) >= 3:
                        return _preserve_case(word, corrected)
                    return _preserve_case(word, corrected)
                return None

    # (4) Single-word fallback
    suggestions = sym_spell.lookup(
        target_lower, Verbosity.CLOSEST,
        max_edit_distance=MAX_EDIT_DISTANCE, include_unknown=False,
    )
    if not suggestions:
        return None
    corrected = suggestions[0].term
    if corrected == target_lower:
        return None
    return _preserve_case(word, corrected)


# ============================================================
#  SMART PUNCTUATION (feature 8)
# ============================================================
def should_capitalize() -> bool:
    """Return True if the next word should start capitalized."""
    with state_lock:
        if not context_words:
            return True
        last = context_words[-1]
    return last.endswith((".", "!", "?"))


# ============================================================
#  DEEP LEARNING PREDICTION (features 6, 13)
# ============================================================
def predict_next_words():
    """Return list of (word, confidence_margin) tuples."""
    if not _model_ready.is_set() or _model is None:
        return []
    with state_lock:
        if not context_words:
            return []
        prompt = " ".join(list(context_words)[-PREDICTION_CONTEXT:])

    # App-aware priming (feature 6)
    app_hint = _get_app_context()
    for key, prefix in APP_HINTS.items():
        if key in app_hint and prefix:
            prompt = prefix + prompt
            break

    try:
        inputs = _tokenizer(prompt, return_tensors="pt")
        with torch.no_grad():
            logits = _model(**inputs).logits[0, -1, :]
        topk = torch.topk(logits, PREDICTION_TOP_K)
        top_ids = topk.indices.tolist()
        top_vals = topk.values.tolist()

        # Confidence gate (feature 13): only show if top-1 vs top-2 margin is large
        if len(top_vals) >= 2 and (top_vals[0] - top_vals[1]) < CONFIDENCE_GATE:
            logger.debug("Prediction suppressed: low confidence")
            return []

        out, seen = [], set()
        for tid in top_ids:
            w = _tokenizer.decode([tid]).strip().lower()
            if w and all(c.isalpha() or c == "'" for c in w) and len(w) >= 2:
                if w not in seen:
                    seen.add(w)
                    out.append(w)
        return out
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        return []


def show_prediction():
    global pending_prediction
    if not enabled_prediction:
        return
    preds = predict_next_words()
    if not preds:
        return
    preds.sort(key=lambda w: -personal_freq.get(w, 0))
    with state_lock:
        pending_prediction = preds[0]
    logger.info(f"[Prediction] {', '.join(preds[:3])}   (Ctrl+Space = accept)")


# ============================================================
#  KEY SIMULATION
# ============================================================
def _tap(key):
    ctrl.press(key)
    ctrl.release(key)


def type_string(text: str):
    for ch in text:
        if ch == "\n":
            _tap(keyboard.Key.enter)
        elif ch == "\t":
            _tap(keyboard.Key.tab)
        else:
            ctrl.type(ch)
        time.sleep(TYPE_DELAY)


def replace_word(original: str, boundary: str, corrected: str):
    global ignore_count
    n_backspaces = len(original) + len(boundary)
    n_typed      = len(corrected) + len(boundary)
    with ignore_lock:
        ignore_count += n_backspaces + n_typed

    time.sleep(SETTLE_BEFORE_DELETE)
    for _ in range(n_backspaces):
        _tap(keyboard.Key.backspace)
        time.sleep(BACKSPACE_DELAY)
    type_string(corrected + boundary)
    time.sleep(0.02)


# ============================================================
#  WORD PIPELINE
# ============================================================
def process_completed_word(word: str, boundary: str):
    global pending_prediction, last_correction

    final_word = word

    if enabled_autocorrect and not _is_excluded_app():
        correction = get_contextual_correction(word)
        if correction and correction != word:
            logger.info(f"[Autocorrect] '{word}{boundary}' -> '{correction}{boundary}'")
            replace_word(word, boundary, correction)
            # Record for undo (feature 2)
            last_correction = (word, correction, boundary, time.time())
            personal_freq[correction.lower()] += 1
            final_word = correction
        elif word:
            personal_freq[word.lower()] += 1

    # Smart punctuation: capitalize next word if needed (feature 8)
    if final_word and should_capitalize() and final_word[0].islower():
        final_word = final_word.capitalize()

    with state_lock:
        pending_prediction = None
        if final_word:
            context_words.append(final_word.lower())

    show_prediction()


def dispatch(word: str, boundary: str):
    threading.Thread(
        target=process_completed_word,
        args=(word, boundary),
        daemon=True,
    ).start()


# ============================================================
#  KEY HANDLER
# ============================================================
def on_press(key):
    global ignore_count

    with ignore_lock:
        if ignore_count > 0:
            ignore_count -= 1
            return

    if not enabled_autocorrect and not enabled_prediction:
        return

    try:
        char = key.char
    except AttributeError:
        char = None

    if char is not None:
        if char in BOUNDARY_CHARS:
            if buffer:
                word = "".join(buffer)
                buffer.clear()
                dispatch(word, char)
            return
        buffer.append(char)
        return

    if key in BOUNDARY_KEYS:
        boundary = BOUNDARY_KEYS[key]
        if buffer:
            word = "".join(buffer)
            buffer.clear()
            dispatch(word, boundary)
        return

    if key == keyboard.Key.backspace:
        if buffer:
            buffer.pop()
        return

    if key == keyboard.Key.esc:
        buffer.clear()
        with state_lock:
            pending_prediction = None
        return


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
    global enabled_prediction, pending_prediction
    enabled_prediction = not enabled_prediction
    with state_lock:
        pending_prediction = None
    logger.info(f"[Prediction] {'ON' if enabled_prediction else 'OFF'}")
    _refresh_tray()


def accept_prediction():
    global pending_prediction, ignore_count
    with state_lock:
        word = pending_prediction
        pending_prediction = None
    if not word:
        return
    with ignore_lock:
        ignore_count += len(word) + 1
    type_string(word + " ")
    logger.info(f"[Prediction] accepted: '{word}'")


def undo_last_correction():
    """Restore the original word if within the timeout (feature 2)."""
    global last_correction, ignore_count
    if not last_correction:
        logger.info("[Undo] Nothing to undo.")
        return
    original, corrected, boundary, ts = last_correction
    if time.time() - ts > UNDO_TIMEOUT:
        logger.info("[Undo] Timeout exceeded.")
        last_correction = None
        return

    logger.info(f"[Undo] Restoring '{original}'")
    # Record the wrong correction so we don't repeat it (feature 11)
    with state_lock:
        ctx = list(context_words)[-1] if context_words else ""
    wrong_corrections[f"{ctx}|{original.lower()}"] += 1

    # Delete the correction+boundary, retype the original+boundary
    global ignore_count
    n_backspaces = len(corrected) + len(boundary)
    n_typed      = len(original) + len(boundary)
    with ignore_lock:
        ignore_count += n_backspaces + n_typed

    time.sleep(SETTLE_BEFORE_DELETE)
    for _ in range(n_backspaces):
        _tap(keyboard.Key.backspace)
        time.sleep(BACKSPACE_DELAY)
    type_string(original + boundary)

    last_correction = None


def grammar_check():
    threading.Thread(target=run_grammar_check, daemon=True).start()


def show_help():
    """Show hotkey cheat sheet (tray notification, feature 1)."""
    text = (
        "Ctrl+Shift+A  toggle autocorrect\n"
        "Ctrl+Shift+P  toggle prediction\n"
        "Ctrl+Space    accept prediction\n"
        "Ctrl+Shift+Z  undo last correction\n"
        "Ctrl+Shift+G  grammar check"
    )
    logger.info("Hotkeys:\n" + text)
    if tray_icon and _HAS_TRAY:
        try:
            tray_icon.notify(text, "Autocorrect Hotkeys")
        except Exception:
            pass


# ============================================================
#  START-AT-LOGON (Task Scheduler) — feature 1
# ============================================================
def _task_exists():
    try:
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root = scheduler.GetFolder("\\")
        root.GetTask(TASK_NAME)
        return True
    except Exception:
        return False


def enable_start_at_logon():
    """Create a Task Scheduler entry that runs this script at logon."""
    try:
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root = scheduler.GetFolder("\\")

        task_def = scheduler.NewTask(0)
        task_def.RegistrationInfo.Description = "Global Autocorrect"
        task_def.Settings.Enabled = True
        task_def.Settings.ExecutionTimeLimit = "PT0S"
        task_def.Settings.DisallowStartIfOnBatteries = False
        task_def.Settings.StopIfGoingOnBatteries = False

        # Trigger: at logon (TASK_TRIGGER_LOGON = 9)
        trigger = task_def.Triggers.Create(9)
        trigger.UserId = os.environ.get("USERNAME", "")

        # Action: run pythonw.exe with this script
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not pathlib.Path(pythonw).exists():
            pythonw = sys.executable
        action = task_def.Actions.Create(0)
        action.Path = pythonw
        action.Arguments = f'"{os.path.abspath(__file__)}"'
        action.WorkingDirectory = str(pathlib.Path(__file__).parent)

        # TASK_CREATE_OR_UPDATE = 6, TASK_LOGON_INTERACTIVE_TOKEN = 3
        root.RegisterTaskDefinition(
            TASK_NAME, task_def, 6, None, None, 3,
        )
        logger.info("Start at logon ENABLED.")
        return True
    except Exception as e:
        logger.error(f"Failed to enable start-at-logon: {e}")
        return False


def disable_start_at_logon():
    """Remove the Task Scheduler entry."""
    try:
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root = scheduler.GetFolder("\\")
        root.DeleteTask(TASK_NAME, 0)
        logger.info("Start at logon DISABLED.")
        return True
    except Exception as e:
        logger.error(f"Failed to disable start-at-logon: {e}")
        return False


def _is_start_at_logon_enabled() -> bool:
    return _task_exists()


def toggle_start_at_logon(icon, item):
    if _is_start_at_logon_enabled():
        disable_start_at_logon()
    else:
        enable_start_at_logon()
    _refresh_tray()


# ============================================================
#  SYSTEM TRAY ICON (feature 1)
# ============================================================
def _make_icon_image(color: str):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color)
    # Draw a small "a" in the centre
    d.text((22, 14), "a", fill="white")
    return img


def _tray_menu():
    return pystray.Menu(
        pystray.MenuItem(
            "Autocorrect",
            lambda icon, item: toggle_autocorrect(),
            checked=lambda item: enabled_autocorrect,
        ),
        pystray.MenuItem(
            "Prediction",
            lambda icon, item: toggle_prediction(),
            checked=lambda item: enabled_prediction,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start at logon",
            toggle_start_at_logon,
            checked=lambda item: _is_start_at_logon_enabled(),
        ),
        pystray.MenuItem("Show hotkeys", lambda icon, item: show_help()),
        pystray.MenuItem("Open log", lambda icon, item: _open_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: _quit_from_tray(icon)),
    )


def _open_log():
    try:
        os.startfile(str(LOG_FILE))
    except Exception as e:
        logger.error(f"Could not open log: {e}")


def _quit_from_tray(icon):
    logger.info("Quit requested from tray")
    icon.stop()
    shutdown()


def _refresh_tray():
    """Force the tray menu to re-evaluate checked states."""
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
        "autocorrect",
        _make_icon_image(color),
        "Global Autocorrect",
        menu=_tray_menu(),
    )
    tray_icon.run()


# ============================================================
#  GRACEFUL SHUTDOWN (feature 18)
# ============================================================
_shutdown_done = threading.Event()


def shutdown(*_args):
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    logger.info("Shutting down...")
    save_personal_data()
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass
    logging.shutdown()


# atexit runs when the interpreter exits normally
atexit.register(shutdown)


def _signal_handler(signum, frame):
    logger.info(f"Signal {signum} received; shutting down.")
    shutdown()
    sys.exit(0)


# ============================================================
#  PERIODIC SAVE (feature 3)
# ============================================================
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
    global listener

    logger.info("=" * 60)
    logger.info("  Global Autocorrect + Prediction — v7")
    logger.info("=" * 60)
    logger.info("  Ctrl+Shift+A   toggle autocorrect")
    logger.info("  Ctrl+Shift+P   toggle prediction")
    logger.info("  Ctrl+Space     accept prediction")
    logger.info("  Ctrl+Shift+Z   undo last correction")
    logger.info("  Ctrl+Shift+G   grammar check")
    logger.info("=" * 60)

    # Load persisted state (feature 3)
    load_personal_data()

    # Fetch common typos in background (feature 4)
    threading.Thread(target=fetch_common_typos, daemon=True).start()

    # Pre-load grammar tool in background (feature 9)
    threading.Thread(target=get_grammar_tool, daemon=True).start()

    # Start the system tray icon (feature 1)
    if _HAS_TRAY:
        threading.Thread(target=_tray_thread, daemon=True).start()
    else:
        logger.warning("pystray not installed; tray icon disabled")

    # Periodic personal dictionary save (feature 3)
    threading.Thread(target=_periodic_save, daemon=True).start()

    # Register signal handlers (feature 18)
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        pass   # not on the main thread

    # Register hotkeys
    hotkeys = {
        TOGGLE_AUTOCORRECT: toggle_autocorrect,
        TOGGLE_PREDICTION:  toggle_prediction,
        ACCEPT_PREDICTION:  accept_prediction,
        UNDO_CORRECTION:    undo_last_correction,
        GRAMMAR_CHECK:      grammar_check,
    }
    hotkey = keyboard.GlobalHotKeys(hotkeys)
    hotkey.start()

    # Global keyboard listener
    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()