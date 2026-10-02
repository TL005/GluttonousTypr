"""
EnviousTypr — v9.7
Global autocorrect + deep-learning prediction for Windows.
Multi-language support: English, French (+ any language with a SyMSpell
dictionary; extra dictionaries can be dropped into ~/.gluttonoustypr/dicts/).

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
  Ctrl+Shift+T   cycle input language (en -> fr -> ...)

Language selection:
  - Auto-detected from the keyboard layout of the focused window (per-word),
    unless a fixed language is configured.
  - Config file: ~/.gluttonoustypr/config.json
      {"language": "auto"}   or   "en"   or   "fr"
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

APP_VERSION            = "9.7"

LOG_FILE               = APP_DIR / "gluttonoustypr.log"
PERSONAL_DICT_FILE     = APP_DIR / "personal_dict.json"
TYPO_CACHE_FILE        = APP_DIR / "common_typos.json"
WRONG_CORRECTIONS_FILE = APP_DIR / "wrong_corrections.json"
NAME_WHITELIST_FILE    = APP_DIR / "names.txt"
APP_CONTEXT_DIR        = APP_DIR / "app_contexts"
LORA_ADAPTER_DIR       = APP_DIR / "lora_adapter"
LORA_DATA_FILE         = APP_DIR / "personal_corpus.txt"
CONFIG_FILE            = APP_DIR / "config.json"
DICTS_DIR              = APP_DIR / "dicts"

APP_CONTEXT_DIR.mkdir(exist_ok=True)
DICTS_DIR.mkdir(exist_ok=True)

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
logger.info("EnviousTypr v9.7 starting")
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
_loaded_model_name = None


def _load_dl_model(model_name="distilgpt2"):
    """Load (or reload for another language) the causal LM used for prediction."""
    global _model, _tokenizer, _use_onnx, _loaded_model_name
    if _loaded_model_name == model_name and _model is not None:
        _model_ready.set()
        return
    _model_ready.clear()
    _model = None
    _tokenizer = None
    try:
        logger.info(f"Loading prediction model '{model_name}' "
                    "(first run downloads ~330 MB)...")
        from transformers import GPT2TokenizerFast, GPT2LMHeadModel
        _tokenizer = GPT2TokenizerFast.from_pretrained(model_name)
        try:
            from optimum.onnxruntime import ORTModelForCausalLM
            _model = ORTModelForCausalLM.from_pretrained(model_name, export=True)
            _use_onnx = True
            logger.info("ONNX Runtime model loaded.")
        except Exception as e:
            logger.warning(f"ONNX unavailable ({e}); using PyTorch")
            import torch
            torch.set_num_threads(4)
            _model = GPT2LMHeadModel.from_pretrained(model_name)
            _model.eval()
        _loaded_model_name = model_name
        logger.info(f"Prediction model ready: {model_name} (ONNX={_use_onnx}).")
    except Exception as e:
        logger.error(f"Deep learning unavailable: {e}")
    finally:
        _model_ready.set()


def _start_model_loading(lang=None):
    cfg = LANGUAGE_CONFIG.get(lang or get_active_language(),
                              LANGUAGE_CONFIG[DEFAULT_LANGUAGE])
    threading.Thread(target=_load_dl_model, args=(cfg["gpt_model"],),
                     daemon=True).start()


def _ensure_model_for_language(lang):
    """Reload the prediction model in the background when the language
    switches to one that uses a different causal LM."""
    cfg = LANGUAGE_CONFIG.get(lang, LANGUAGE_CONFIG[DEFAULT_LANGUAGE])
    if _loaded_model_name != cfg["gpt_model"]:
        _start_model_loading(lang)


# ============================================================
#  SPELL CHECKER (multi-language)
# ============================================================
# Supported languages. Each entry describes:
#   label          — human-readable name (used in tray / notifications)
#   ltm_code       — LanguageTool code for grammar checking
#   gpt_model      — causal LM used for predictive text
#   builtin_dict   — dictionary shipped inside the symspellpy package
#   builtin_bigram — optional bigram dictionary shipped inside symspellpy
#   dict_assets    — (frequency, [bigrams]) inside the HF dataset below
# French ships as a downloadable SyMSpell frequency dictionary
# (maartendefruytier/symspellingdictionaries), cached locally in
# ~/.gluttonoustypr/dicts/ on first use. Users may also drop their own
# "<lang>_frequency.txt" files into that folder for other languages.
LANGUAGE_CONFIG = {
    "en": {
        "label": "English",
        "ltm_code": "en-US",
        "gpt_model": "distilgpt2",
        "builtin_dict": "frequency_dictionary_en_82_765.txt",
        "builtin_bigram": "frequency_bigramdictionary_en_243_342.txt",
        "dict_assets": None,
    },
    "fr": {
        "label": "Français",
        "ltm_code": "fr",
        "gpt_model": "dbmdz/distilbert-fr-gpt2-small",
        "builtin_dict": None,
        "builtin_bigram": None,
        "dict_assets": (
            "french_frequency_dictionary.txt",
            ["french_bigram_dictionary.txt"],
        ),
    },
    "de": {
        "label": "Deutsch",
        "ltm_code": "de-DE",
        "gpt_model": "dbmdz/gpt2-duits-small",
        "builtin_dict": None,
        "builtin_bigram": None,
        "dict_assets": (
            "german_frequency_dictionary.txt",
            ["german_bigram_dictionary.txt"],
        ),
    },
    "es": {
        "label": "Español",
        "ltm_code": "es",
        "gpt_model": "dbmdz/gpt2-esperanto_small",
        "builtin_dict": None,
        "builtin_bigram": None,
        "dict_assets": (
            "spanish_frequency_dictionary.txt",
            ["spanish_bigram_dictionary.txt"],
        ),
    },
}
DICT_DATASET = "maartendefruytier/symspellingdictionaries"
DEFAULT_LANGUAGE = "en"

_lang_lock = threading.Lock()
supported_languages = [DEFAULT_LANGUAGE]
_sym_spell_by_lang = {}          # lang -> SymSpell instance
_known_words_by_lang = {}        # lang -> set of lowercase words
_personal_dicts_by_lang = {}     # lang -> {typo: correction} (internet cache)
_language_mode = "auto"          # "auto" or an explicit language code
_current_layout_lang = DEFAULT_LANGUAGE


def _download_dictionary(lang):
    """Fetch a language's SyMSpell dictionaries from HF (cached on disk)."""
    cfg = LANGUAGE_CONFIG[lang]
    assets = cfg.get("dict_assets")
    if not assets:
        return None
    freq_asset, bigram_assets = assets[0], (assets[1] or [])
    dest = DICTS_DIR / f"{lang}_frequency.txt"
    if not (dest.exists() and dest.stat().st_size > 100_000):
        try:
            from huggingface_hub import hf_hub_download
            path = hf_hub_download(repo_id=DICT_DATASET, filename=freq_asset)
            shutil.copyfile(path, dest)
            logger.info("[%s] Dictionary downloaded to %s", lang, dest)
        except Exception as e:
            logger.error(f"[{lang}] dictionary download failed: {e}")
            return None
    for b in bigram_assets:
        bdest = DICTS_DIR / f"{lang}_{b}"
        if not (bdest.exists() and bdest.stat().st_size > 1000):
            try:
                from huggingface_hub import hf_hub_download
                path = hf_hub_download(repo_id=DICT_DATASET, filename=b)
                shutil.copyfile(path, bdest)
            except Exception as e:
                logger.warning(f"[{lang}] bigram '{b}' skipped: {e}")
    return dest


def _find_user_dict_file(lang):
    """Optional user-provided dictionary: dicts/<lang>_frequency.txt."""
    for candidate in (DICTS_DIR / f"{lang}_frequency.txt",
                      DICTS_DIR / f"{lang}.txt"):
        if candidate.exists():
            return candidate
    return None


def _load_language_dictionary(lang):
    """Load (or reload) the spell-checker for one language. Thread-safe."""
    cfg = LANGUAGE_CONFIG[lang]
    ss = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)

    dict_path = _find_user_dict_file(lang)
    if dict_path is None and cfg["builtin_dict"]:
        dict_path = importlib.resources.files("symspellpy") / cfg["builtin_dict"]
    if dict_path is None and cfg.get("dict_assets"):
        dict_path = _download_dictionary(lang)
    if dict_path is None or not os.path.exists(str(dict_path)):
        raise FileNotFoundError(f"No dictionary available for '{lang}'")

    ss.load_dictionary(str(dict_path), term_index=0, count_index=1)

    bigram_path = None
    if cfg["builtin_bigram"]:
        bigram_path = (importlib.resources.files("symspellpy")
                       / cfg["builtin_bigram"])
    elif cfg["dict_assets"] and cfg["dict_assets"][1]:
        for asset in cfg["dict_assets"][1]:
            candidate = DICTS_DIR / f"{lang}_{asset}"
            if candidate.exists():
                bigram_path = candidate
                break
    if bigram_path and os.path.exists(str(bigram_path)):
        try:
            ss.load_bigram_dictionary(str(bigram_path),
                                      term_index=0, count_index=2)
        except Exception as e:
            logger.warning(f"[{lang}] bigram dictionary skipped: {e}")

    known = set()
    try:
        with open(str(dict_path), encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if parts:
                    known.add(parts[0].lower())
    except Exception:
        pass

    with _lang_lock:
        _sym_spell_by_lang[lang] = ss
        _known_words_by_lang[lang] = known
        _personal_dicts_by_lang.setdefault(lang, {})
    logger.info(f"[{lang}] Dictionary: {ss.word_count:,} words")


def _lazy_load_language(lang):
    """Background thread: download + load one language's dictionary so the
    tray menu can list it immediately without delaying startup."""
    try:
        _load_language_dictionary(lang)
        with _lang_lock:
            if lang not in supported_languages:
                supported_languages.append(lang)
        logger.info(f"[{lang}] Lazy dictionary load complete "
                    f"({LANGUAGE_CONFIG[lang]['label']}).")
        _refresh_tray()
    except Exception as e:
        logger.warning(f"Language '{lang}' unavailable: {e}")


def _start_lazy_language_loads():
    pending = [l for l in LANGUAGE_CONFIG if l not in supported_languages]
    for lang in pending:
        threading.Thread(target=_lazy_load_language, args=(lang,),
                         daemon=True, name=f"lang-{lang}").start()


def _init_languages():
    """Load every language whose dictionary is already available locally
    (bundled or cached); never crash. Missing dictionaries are fetched
    later by `_start_lazy_language_loads()`."""
    global supported_languages
    loaded = []
    for lang in LANGUAGE_CONFIG:
        cfg = LANGUAGE_CONFIG[lang]
        cached = cfg["builtin_dict"] is None and not (
            DICTS_DIR / f"{lang}_frequency.txt").exists() and not (
            DICTS_DIR / f"{lang}.txt").exists() and (
            cfg.get("dict_assets") is None or
            not (DICTS_DIR / cfg["dict_assets"][0]).exists())
        if cached:
            continue  # nothing local yet -> lazy-load after startup
        try:
            _load_language_dictionary(lang)
            loaded.append(lang)
        except Exception as e:
            logger.warning(f"Language '{lang}' unavailable: {e}")
    if not loaded:
        loaded = [DEFAULT_LANGUAGE]
    supported_languages = loaded
    logger.info("Languages ready: " + ", ".join(
        f"{l} ({LANGUAGE_CONFIG[l]['label']})" for l in loaded))


_init_languages()


def get_sym_spell(lang=None):
    with _lang_lock:
        return _sym_spell_by_lang.get(lang or get_active_language(),
                                      _sym_spell_by_lang.get(DEFAULT_LANGUAGE))


def _is_known_word(w, lang=None):
    with _lang_lock:
        words = _known_words_by_lang.get(lang or get_active_language(),
                                         _known_words_by_lang.get(DEFAULT_LANGUAGE, set()))
    return w.lower() in words


# ============================================================
#  KEYBOARD-LAYOUT LANGUAGE AUTO-DETECTION (Windows)
# ============================================================
_KLID_KEYBOARD = 0x04
_HKL_TO_LANG = {
    # English variants
    "0409": "en", "1009": "en", "0c09": "en", "4009": "en", "0809": "en",
    # French variants
    "040c": "fr", "080c": "fr", "200c": "fr", "0413": "fr", "0c0c": "fr",
    # German variants
    "0407": "de", "0c07": "de", "040a_de": "de",
    # Spanish variants
    "040a": "es", "0c0a": "es", "2c0a": "es", "400a": "es", "600a": "es",
}
_PRIMARY_LANG_TO_CODE = {0x09: "en", 0x0C: "fr", 0x07: "de", 0x0A: "es"}


def detect_keyboard_language():
    """Return 'fr' when the focused window uses a French keyboard layout."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        if not tid:
            return None
        hkl = user32.GetKeyboardLayout(tid)
        if not hkl:
            return None
        langid = hkl & 0xFFFF
        hexcode = f"{langid:04x}"
        if hexcode in _HKL_TO_LANG:
            return _HKL_TO_LANG[hexcode]
        # Fall back to the primary-language sub-tag of the LANGID
        code = _PRIMARY_LANG_TO_CODE.get(langid & 0x3FF)
        if code in supported_languages:
            return code
    except Exception as e:
        logger.debug(f"Keyboard-layout detection error: {e}")
    return None


# ============================================================
#  UI TRANSLATIONS (tray menu, hotkey notification)
# ============================================================
TRANSLATIONS = {
    "en": {
        "autocorrect": "Autocorrect",
        "prediction": "Prediction",
        "language": "Language",
        "start_at_logon": "Start at logon",
        "fine_tune": "Fine-tune (LoRA)",
        "show_hotkeys": "Show hotkeys",
        "open_log": "Open log",
        "quit": "Quit",
        "hotkeys_title": "EnviousTypr Hotkeys",
        "hotkeys_body": (
            "Ctrl+Shift+A  toggle autocorrect\n"
            "Ctrl+Shift+P  toggle prediction\n"
            "Ctrl+Space    accept prediction\n"
            "Shift+`       undo last correction (within 5 s)\n"
            "Ctrl+Shift+G  grammar check\n"
            "Ctrl+Shift+T  switch language\n"
            "Ctrl+Shift+L  LoRA fine-tune"
        ),
        "autocorrect_on": "[Autocorrect] ON",
        "autocorrect_off": "[Autocorrect] OFF",
        "prediction_on": "[Prediction] ON",
        "prediction_off": "[Prediction] OFF",
        "language_changed": "Input language: {}",
        "grammar_none": "[Grammar] No issues.",
    },
    "fr": {
        "autocorrect": "Correction automatique",
        "prediction": "Prédiction",
        "language": "Langue",
        "start_at_logon": "Démarrer à l'ouverture de session",
        "fine_tune": "Ajustement (LoRA)",
        "show_hotkeys": "Afficher les raccourcis",
        "open_log": "Ouvrir le journal",
        "quit": "Quitter",
        "hotkeys_title": "Raccourcis EnviousTypr",
        "hotkeys_body": (
            "Ctrl+Maj+A    activer/désactiver la correction automatique\n"
            "Ctrl+Maj+P    activer/désactiver la prédiction\n"
            "Ctrl+Espace   accepter la prédiction\n"
            "Maj+`         annuler la dernière correction (5 s)\n"
            "Ctrl+Maj+G    vérification grammaticale\n"
            "Ctrl+Maj+T    changer de langue\n"
            "Ctrl+Maj+L    ajustement LoRA"
        ),
        "autocorrect_on": "[Correction auto] activée",
        "autocorrect_off": "[Correction auto] désactivée",
        "prediction_on": "[Prédiction] activée",
        "prediction_off": "[Prédiction] désactivée",
        "language_changed": "Langue de saisie : {}",
        "grammar_none": "[Grammaire] Aucun problème.",
    },
    "de": {
        "autocorrect": "Autokorrektur",
        "prediction": "Vorhersage",
        "language": "Sprache",
        "start_at_logon": "Beim Anmelden starten",
        "fine_tune": "Feinabstimmung (LoRA)",
        "show_hotkeys": "Tastenkürzel anzeigen",
        "open_log": "Protokoll öffnen",
        "quit": "Beenden",
        "hotkeys_title": "EnviousTypr-Tastenkürzel",
        "hotkeys_body": (
            "Strg+Umsch+A  Autokorrektur ein/aus\n"
            "Strg+Umsch+P  Vorhersage ein/aus\n"
            "Strg+Leertaste  Vorhersage übernehmen\n"
            "Umsch+`  letzte Korrektur rückgängig (5 s)\n"
            "Strg+Umsch+G  Grammatikprüfung\n"
            "Strg+Umsch+T  Sprache wechseln\n"
            "Strg+Umsch+L  LoRA-Feinabstimmung"
        ),
        "autocorrect_on": "[Autokorrektur] EIN",
        "autocorrect_off": "[Autokorrektur] AUS",
        "prediction_on": "[Vorhersage] EIN",
        "prediction_off": "[Vorhersage] AUS",
        "language_changed": "Eingabesprache: {}",
        "grammar_none": "[Grammatik] Keine Probleme.",
    },
    "es": {
        "autocorrect": "Autocorrección",
        "prediction": "Predicción",
        "language": "Idioma",
        "start_at_logon": "Iniciar al abrir sesión",
        "fine_tune": "Ajuste fino (LoRA)",
        "show_hotkeys": "Mostrar atajos",
        "open_log": "Abrir registro",
        "quit": "Salir",
        "hotkeys_title": "Atajos de EnviousTypr",
        "hotkeys_body": (
            "Ctrl+Mayús+A  activar/desactivar autocorrección\n"
            "Ctrl+Mayús+P  activar/desactivar predicción\n"
            "Ctrl+Espacio  aceptar predicción\n"
            "Mayús+`       deshacer última corrección (5 s)\n"
            "Ctrl+Mayús+G  revisión gramatical\n"
            "Ctrl+Mayús+T  cambiar idioma\n"
            "Ctrl+Mayús+L  ajuste fino LoRA"
        ),
        "autocorrect_on": "[Autocorrección] ACTIVADA",
        "autocorrect_off": "[Autocorrección] DESACTIVADA",
        "prediction_on": "[Predicción] ACTIVADA",
        "prediction_off": "[Predicción] DESACTIVADA",
        "language_changed": "Idioma de entrada: {}",
        "grammar_none": "[Gramática] Sin problemas.",
    },
}


def t(key, lang=None):
    """Translate a UI string key using the given/active language."""
    lang = lang or get_active_language()
    table = TRANSLATIONS.get(lang) or TRANSLATIONS[DEFAULT_LANGUAGE]
    return table.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)


# ============================================================
#  CONFIG FILE (language preference)
# ============================================================
def load_config():
    global _language_mode
    mode = DEFAULT_LANGUAGE
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            mode = str(data.get("language", DEFAULT_LANGUAGE)).lower()
        except Exception as e:
            logger.error(f"config.json load failed: {e}")
    if mode == "auto":
        _language_mode = "auto"
    elif mode in supported_languages:
        _language_mode = mode
    else:
        logger.warning(f"Unknown language '{mode}' in config; using auto")
        _language_mode = "auto"


def save_config():
    try:
        CONFIG_FILE.write_text(
            json.dumps({"language": _language_mode}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"config.json save failed: {e}")


def set_language_mode(mode):
    global _language_mode
    if mode != "auto" and mode not in LANGUAGE_CONFIG:
        return False
    _language_mode = mode
    if mode != "auto" and mode not in supported_languages:
        # Dictionary still downloading in the background: kick off an
        # immediate (re)load so correction works as soon as it lands.
        threading.Thread(target=_lazy_load_language, args=(mode,),
                         daemon=True, name=f"lang-{mode}").start()
    save_config()
    return True


def cycle_language():
    """Ctrl+Shift+T: auto -> every configured language in turn -> auto.
    Matches the order shown in the tray Language submenu."""
    options = ["auto"] + list(LANGUAGE_CONFIG.keys())
    try:
        idx = options.index(_language_mode)
    except ValueError:
        idx = 0
    nxt = options[(idx + 1) % len(options)]
    set_language_mode(nxt)
    if nxt == "auto":
        msg = t("language_changed").format("auto (keyboard layout)")
        _ensure_model_for_language(get_active_language())
    else:
        msg = t("language_changed", nxt).format(LANGUAGE_CONFIG[nxt]["label"])
        _ensure_model_for_language(nxt)
    logger.info(f"[Language] {msg}")
    if tray_icon and _HAS_TRAY:
        try:
            tray_icon.notify(msg, t("hotkeys_title"))
        except Exception:
            pass
    _refresh_tray()


def get_language_mode():
    return _language_mode


def _layout_watcher():
    """In auto mode, track the foreground window's keyboard layout so each
    word is corrected with the matching language."""
    global _current_layout_lang
    last_model_lang = None
    while True:
        try:
            if _shutdown_done.is_set():
                break
            if _language_mode == "auto":
                detected = detect_keyboard_language() or DEFAULT_LANGUAGE
                if detected not in supported_languages:
                    detected = DEFAULT_LANGUAGE
                _current_layout_lang = detected
                if detected != last_model_lang:
                    last_model_lang = detected
                    _ensure_model_for_language(detected)
            time.sleep(0.4)
        except Exception as e:
            logger.debug(f"layout watcher error: {e}")
            time.sleep(2.0)


def get_active_language():
    """Language used for the word currently being typed."""
    if _language_mode != "auto":
        return _language_mode
    return _current_layout_lang


# ============================================================
#  PER-LANGUAGE CORRECTION TABLES
# ============================================================
class _LangTables:
    def __init__(self, adjacent=None, typos=None, contractions=None,
                 slang_whitelist=None, slang_replacements=None,
                 homophones=None, confused=None):
        self.keyboard_adjacent = adjacent or {}
        self.typos = typos or {}
        self.special_contractions = contractions or {}
        self.slang_whitelist = slang_whitelist or set()
        self.slang_replacements = slang_replacements or {}
        self.homophone_groups = homophones or []
        self.confused_words = confused or {}

    def build(self):
        self.common_typos = dict(self.typos)
        self._homophone_index = {}
        for g in self.homophone_groups:
            for w in g:
                self._homophone_index[w] = g


ENGLISH_TABLES = None  # built after the lookup tables below


# ============================================================
#  LOOKUP TABLES
# ============================================================
KEYBOARD_ADJACENT_EN = {
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

FALLBACK_TYPOS_EN = {
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

CONFUSED_WORDS_EN = {
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

SLANG_WHITELIST_EN = {
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

SLANG_REPLACEMENTS_EN = {
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
HOMOPHONE_GROUPS_EN = [
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
def disambiguate_homophone(word, sentence_prefix, tables=None):
    if not ENABLE_HOMOPHONE:
        return None
    tables = tables or get_tables()
    lower = word.lower().rstrip(".,!?;:")
    group = tables._homophone_index.get(lower)
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


_SPECIAL_CONTRACTIONS_EN = {
    "im": "I'm", "ive": "I've", "ill": "I'll", "id": "I'd",
    "youre": "you're", "youve": "you've", "youll": "you'll", "youd": "you'd",
    "weve": "we've", "theyre": "they're", "theyve": "they've", "theyll": "they'll",
    "hes": "he's", "shes": "she's", "aint": "ain't",
}


# ============================================================
#  FRENCH LOOKUP TABLES
# ============================================================
KEYBOARD_ADJACENT_FR = {
    # Touch-typing transpositions (AZERTY-friendly entries)
    "poour": "pour", "poru": "pour", "pouur": "pour",
    "qeu": "que", "euq": "que", "qeue": "que",
    "etd": "et", "ted": "est", "ets": "est",
    "paar": "par", "tocut": "tout", "touut": "tout",
    "comse": "comme", "conme": "comme", "pls": "plus",
}

FALLBACK_TYPOS_FR = {
    # Missing accents (common when typing fast without an AZERTY layout)
    "apres": "après", "recuperer": "récupérer", "evaluer": "évaluer",
    "economique": "économique", "telephone": "téléphone",
    "developper": "développer", "debut": "début", "probleme": "problème",
    "theatre": "théâtre", "cinema": "cinéma", "memorie": "mémoire",
    "envirronement": "environnement", "gouvenerment": "gouvernement",
    "difference": "différence", "experiance": "expérience",
    "bibliotheque": "bibliothèque", "sincerite": "sincérité",
    "completelement": "complètement", "evidement": "évidemment",
    "vrayment": "vraiment", "souvant": "souvent", "longtemp": "longtemps",
    "bientot": "bientôt", "biensur": "bien sûr", "pourqoi": "pourquoi",
    "demnin": "demain", "merxi": "merci", "bnjour": "bonjour",
    "bonjout": "bonjour", "comant": "comment", "etre": "être",
    "etres": "êtres", "avbir": "avoir", "fairre": "faire",
    "viendre": "venir", "voullais": "voulais", "cepentant": "cependant",
    "neammoins": "néanmoins", "travial": "travail", "traveil": "travail",
    "argeent": "argent", "famil": "famille", "enfans": "enfants",
    "universiter": "université", "profeseur": "professeur",
    "etuudiant": "étudiant", "garcon": "garçon", "paius": "pays",
    "maisom": "maison", "appartment": "appartement", "chabre": "chambre",
    "cusine": "cuisine", "tabel": "table", "cahise": "chaise",
    "portte": "porte", "fenetre": "fenêtre", "iardjin": "jardin",
    "animos": "animaux", "oissaux": "oiseaux", "viannde": "viande",
    "biere": "bière", "sucsre": "sucre", "dejeuner": "déjeuner",
    "diner": "dîner", "gouter": "goûter", "cosininer": "cuisiner",
    "couter": "coûter", "burriaux": "bureaux", "collegue": "collègue",
    "revnus": "revenus", "impots": "impôts", "ecran": "écran",
    "donnees": "données", "reseau": "réseau", "reseaux": "réseaux",
    "prenom": "prénom", "annee": "année", "annees": "années",
    "siecle": "siècle", "weekend": "week-end", "vacance": "vacances",
    "hotel": "hôtel", "hotels": "hôtels", "metro": "métro",
    "velo": "vélo", "riviere": "rivière", "ocean": "océan",
    "etoiles": "étoiles", "etoile": "étoile", "tempete": "tempête",
    "temperature": "température", "degre": "degré", "degres": "degrés",
    "carre": "carré", "etroit": "étroit", "epais": "épais",
    "leger": "léger", "superieur": "supérieur", "inferieur": "inférieur",
    "special": "spécial", "general": "général", "tres": "très",
    "tot": "tôt", "derriere": "derrière", "malgre": "malgré",
    "excepte": "excepté",
    # Missing apostrophes
    "aujourdhui": "aujourd'hui", "aujourdui": "aujourd'hui",
    "aujordhui": "aujourd'hui", "daucoup": "beaucoup",
    "daccord": "d'accord", "dailleurs": "d'ailleurs",
    "peutetre": "peut-être", "petetre": "peut-être",
    # Common misspellings
    "baucoup": "beaucoup", "beaucoups": "beaucoup",
    "surement": "sûrement", "voila": "voilà", "deja": "déjà",
    "francais": "français", "lanuage": "langage",
    "envireonnement": "environnement", "restautant": "restaurant",
    "librarie": "librairie", "parmis": "parmi", "parmit": "parmi",
    "malgrés": "malgré", "malgres": "malgré",
    "réelement": "réellement", "suvent": "souvent",
    "toujour": "toujours", "toujurs": "toujours", "persone": "personne",
    "maintenants": "maintenant", "scavoit": "savoir",
    "auxautres": "aux autres", "lesquelle": "lesquelles",
    "lequelle": "lesquelles", "d'avantage": "davantage",
}

CONFUSED_WORDS_FR = {
    "a":     {"verb": "a", "preposition": "à"},
    "à":     {"verb": "a", "preposition": "à"},
    "ou":    {"choice": "ou", "place": "où"},
    "où":    {"choice": "ou", "place": "où"},
    "son":   {"possessive": "son", "verb": "sont"},
    "sont":  {"possessive": "son", "verb": "sont"},
    "ces":   {"demonstrative": "ces", "contraction": "c'est"},
    "c'est": {"demonstrative": "ces", "contraction": "c'est"},
    "est":   {"verb": "est", "conjunction": "et"},
    "et":    {"verb": "est", "conjunction": "et"},
    "mes":   {"possessive": "mes", "conjunction": "mais"},
    "mais":  {"possessive": "mes", "conjunction": "mais"},
    "sa":    {"possessive": "sa", "demonstrative": "ça"},
    "ça":    {"possessive": "sa", "demonstrative": "ça"},
    "se":    {"reflexive": "se", "demonstrative": "ce"},
    "ce":    {"reflexive": "se", "demonstrative": "ce"},
}

SLANG_WHITELIST_FR = {
    # Verlan / familier / SMS — never "correct" these
    "wsh", "wesh", "chelou", "relou", "meuf", "reuf", "keums",
    "boloss", "bolosse", "gosse", "gosses", "mec", "pote", "potes",
    "kiff", "kiffer", "kiffe", "zlaté", "chanmé", "bouffon", "clope",
    "bouffe", "bagnole", "frime", "frimer", "matos", "bidoche",
    "vénère", "barjo", "barjot", "taré", "zinzin", "dingue", "gonzesse",
    "teuf", "caillera", "racaille", "rabiot", "sombard", "daron",
    "darons", "mif", "mistos", "chorizo", "bougass", "cramé", "charo",
    "charos", "chill", "chiller", "daba", "grave", "galère", "galeres",
    "bourré", "pépite", "pépites", "swag", "cool", "naze", "bidon",
    "skibidi", "rizz", "sigma", "delulu", "glowup", "bestie",
    "mdr", "ptdr", "bg", "svp", "stp", "dsl", "bisous", "gros",
    "grosse", "lol", "wtf", "omg", "pk", "cv", "cc", "kt", "ki",
    "kon", "waloo", "wallah", "mashallah", "tkt", "kifkif",
}

SLANG_REPLACEMENTS_FR = {
    "weshh": "wesh", "wech": "wesh", "wsch": "wsh", "wss": "wsh",
    "chaelou": "chelou", "chelo": "chelou", "vnere": "vénère",
    "kife": "kiffe", "frerot": "frérot", "bolos": "bolosse",
    "rlou": "relou", "taree": "tarée", "barjou": "barjo",
    "meuuf": "meuf", "db": "daba", "chil": "chill",
}

HOMOPHONE_GROUPS_FR = [
    {"a", "à"},
    {"ou", "où"},
    {"son", "sont"},
    {"ces", "ses", "c'est", "six"},
    {"est", "et", "eux"},
    {"mes", "mais", "met"},
    {"mon", "mont"},
    {"ton", "thon"},
    {"tes", "tais", "test"},
    {"leur", "leurs"},
    {"se", "ce", "ces"},
    {"sa", "ça"},
    {"on", "nom"},
    {"verre", "vert", "vers"},
    {"pain", "pin"},
    {"sans", "cent", "saint"},
    {"fer", "faire"},
    {"croix", "crois"},
    {"foi", "fois"},
    {"rois", "roi"},
    {"soi", "sois", "soit"},
    {"dents", "dans"},
    {"bas", "bah"},
    {"bel", "belle"},
    {"sens", "cent", "sans"},
    {"bal", "balle"},
    {"mets", "met"},
    {"pois", "poi", "puits"},
    {"pot", "peau"},
    {"rond", "fond"},
    {"mer", "mère", "maire"},
    {"pair", "paire", "père"},
    {"terre", "taire"},
    {"air", "aire", "erre", "hair"},
]

_SPECIAL_CONTRACTIONS_FR = {
    "cest": "c'est", "cetait": "c'était", "ctait": "c'était",
    "silvousplait": "s'il vous plaît",
    "aujourdhuis": "aujourd'hui", "quelquuns": "quelqu'un",
    "nullepart": "nulle part", "arriere": "arrière",
    "autoecole": "auto-école", "jusquici": "jusqu'ici",
    "presqueile": "presqu'île", "cella": "cela",
    "dune": "d'une", "jlai": "j'ai", "yena": "y en a",
}


# ============================================================
#  LANGUAGE REGISTRY
# ============================================================
ENGLISH_TABLES = _LangTables(
    adjacent=KEYBOARD_ADJACENT_EN,
    typos=FALLBACK_TYPOS_EN,
    contractions=_SPECIAL_CONTRACTIONS_EN,
    slang_whitelist=SLANG_WHITELIST_EN,
    slang_replacements=SLANG_REPLACEMENTS_EN,
    homophones=HOMOPHONE_GROUPS_EN,
    confused=CONFUSED_WORDS_EN,
)

FRENCH_TABLES = _LangTables(
    adjacent=KEYBOARD_ADJACENT_FR,
    typos=FALLBACK_TYPOS_FR,
    contractions=_SPECIAL_CONTRACTIONS_FR,
    slang_whitelist=SLANG_WHITELIST_FR,
    slang_replacements=SLANG_REPLACEMENTS_FR,
    homophones=HOMOPHONE_GROUPS_FR,
    confused=CONFUSED_WORDS_FR,
)

LANGUAGE_TABLES = {
    "en": ENGLISH_TABLES,
    "fr": FRENCH_TABLES,
}


def get_tables(lang=None):
    """Return the correction tables for a language (falls back to English)."""
    lang = lang or get_active_language()
    return LANGUAGE_TABLES.get(lang, LANGUAGE_TABLES[DEFAULT_LANGUAGE])


# Build derived structures; drop identity typo entries and risky expansions
for _lang_obj in LANGUAGE_TABLES.values():
    _lang_obj.build()
    for _w in list(_lang_obj.common_typos.keys()):
        if _lang_obj.common_typos[_w].lower() == _w:
            del _lang_obj.common_typos[_w]

for _risky in ("its", "were", "well", "id", "ill", "im", "ive",
               "ya", "dat", "dis", "yall"):
    ENGLISH_TABLES.common_typos.pop(_risky, None)

# Per-language personal dictionaries (loaded from disk later)
for _code in LANGUAGE_TABLES:
    _personal_dicts_by_lang.setdefault(_code, {})


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
    """Augment the ENGLISH tables with crowd-sourced typo fixes (Datamuse)."""
    en_tables = LANGUAGE_TABLES["en"]
    if TYPO_CACHE_FILE.exists():
        try:
            cached = json.loads(TYPO_CACHE_FILE.read_text(encoding="utf-8"))
            new_keys = {k: v for k, v in cached.items()
                        if k not in FALLBACK_TYPOS_EN}
            en_tables.common_typos.update(new_keys)
            logger.info(f"Cached typos: {len(new_keys)} new")
            return
        except Exception:
            pass
    logger.info("Fetching typo data from Datamuse...")
    try:
        fetched = {}
        for typo in list(FALLBACK_TYPOS_EN.keys())[:50]:
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
        new_entries = {k: v for k, v in fetched.items()
                       if k not in FALLBACK_TYPOS_EN}
        if new_entries:
            en_tables.common_typos.update(new_entries)
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
CYCLE_LANGUAGE     = "<ctrl>+<shift>+t"

# Undo is handled inline in on_press (see UNDO_KEY_CHAR).
# pynput's GlobalHotKeys cannot match shifted punctuation keys,
# so we listen for the resulting character '~' directly.
UNDO_KEY_CHAR = "~"

TASK_NAME = "EnviousTypr"

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
    contractions = get_tables().special_contractions
    if low in contractions:
        return contractions[low]
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
    lang = get_active_language()
    tables = get_tables(lang)
    spellchecker = get_sym_spell(lang)
    if spellchecker is None:
        return None

    if target_lower in tables.slang_whitelist:
        return None
    if target_lower in tables.slang_replacements:
        return _preserve_case(word, tables.slang_replacements[target_lower])
    if target_lower in name_whitelist:
        return None
    if target_lower in tables.keyboard_adjacent:
        return _preserve_case(word, tables.keyboard_adjacent[target_lower])
    if target_lower in tables.common_typos:
        return _preserve_case(word, tables.common_typos[target_lower])

    with state_lock:
        ctx = list(context_words)[-(CONTEXT_WINDOW - 1):] if CONTEXT_WINDOW > 1 else []
    if ctx:
        ctx_key = f"{ctx[-1]}|{target_lower}"
        if wrong_corrections.get(ctx_key, 0) >= 2:
            return None

    if (ENABLE_HOMOPHONE and tables._homophone_index.get(target_lower)
            and len(target_lower) >= 4):
        h = disambiguate_homophone(word, " ".join(ctx), tables)
        if h and h != target_lower:
            return _preserve_case(word, h)

    phrase = " ".join(ctx + [word]) if ctx else word
    try:
        sug = spellchecker.lookup_compound(
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

    sug = spellchecker.lookup(
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


_grammar_tool_lang = None


def get_grammar_tool():
    """LanguageTool instance for the currently active language (rebuilt on
    language switch)."""
    global _grammar_tool, _grammar_tool_lang
    lang = get_active_language()
    ltm_code = LANGUAGE_CONFIG.get(lang, LANGUAGE_CONFIG[DEFAULT_LANGUAGE])["ltm_code"]
    with _grammar_lock:
        if _grammar_tool is None or _grammar_tool_lang != ltm_code:
            try:
                import language_tool_python
                logger.info(f"Loading LanguageTool ({ltm_code})...")
                _grammar_tool = language_tool_python.LanguageTool(ltm_code)
                _grammar_tool_lang = ltm_code
                logger.info(f"LanguageTool ready ({ltm_code}).")
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
        logger.info(t("grammar_none"))
        return
    for m in matches[:5]:
        logger.info(f"[Grammar] {m.message}")


def grammar_check():
    threading.Thread(target=run_grammar_check, daemon=True).start()


def show_help():
    text = t("hotkeys_body")
    logger.info("Hotkeys:\n" + text)
    if tray_icon and _HAS_TRAY:
        try:
            tray_icon.notify(text, t("hotkeys_title"))
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
        td.RegistrationInfo.Description = "EnviousTypr"
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


def _language_submenu():
    """Radio list: Auto + EVERY configured language (Ctrl+Shift+T cycles
    them). Languages whose dictionary is still downloading in the background
    are shown greyed-out as '(loading…)' and become selectable once ready."""
    items = [pystray.MenuItem(
        "Auto (keyboard layout)",
        lambda i, it: _pick_language("auto"),
        checked=lambda it: get_language_mode() == "auto",
        radio=True,
    )]
    for code, cfg in LANGUAGE_CONFIG.items():
        label = cfg["label"]
        if code not in supported_languages:
            label += "  (loading\u2026)"
        items.append(pystray.MenuItem(
            label,
            (lambda c: lambda i, it: _pick_language(c))(code),
            checked=lambda it, c=code: get_language_mode() == c,
            enabled=(code in supported_languages),
            radio=True,
        ))
    return pystray.Menu(*items)


def _pick_language(mode):
    if set_language_mode(mode):
        if mode != "auto":
            _ensure_model_for_language(mode)
        label = ("auto (keyboard layout)" if mode == "auto"
                 else LANGUAGE_CONFIG[mode]["label"])
        logger.info("[Language] " + t("language_changed").format(label))
    _refresh_tray()


def _tray_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda item: t("autocorrect"),
                         lambda i, it: toggle_autocorrect(),
                         checked=lambda it: enabled_autocorrect),
        pystray.MenuItem(lambda item: t("prediction"),
                         lambda i, it: toggle_prediction(),
                         checked=lambda it: enabled_prediction),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: t("language"), _language_submenu()),
        pystray.MenuItem(lambda item: t("start_at_logon"),
                         toggle_start_at_logon,
                         checked=lambda it: _task_exists()),
        pystray.MenuItem(lambda item: t("fine_tune"),
                         lambda i, it: trigger_lora()),
        pystray.MenuItem(lambda item: t("show_hotkeys"),
                         lambda i, it: show_help()),
        pystray.MenuItem(lambda item: t("open_log"), lambda i, it: _open_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: t("quit"),
                         lambda i, it: _quit_from_tray(i)),
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
        "EnviousTypr v9.7", menu=_tray_menu(),
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
    logger.info("  EnviousTypr v9.7")
    logger.info("=" * 60)
    logger.info("  Ctrl+Shift+A  autocorrect")
    logger.info("  Ctrl+Shift+P  prediction")
    logger.info("  Ctrl+Space    accept prediction")
    logger.info("  Shift+`       undo")
    logger.info("  Ctrl+Shift+G  grammar")
    logger.info("=" * 60)
    for _code in supported_languages:
        _tb = LANGUAGE_TABLES[_code]
        logger.info(
            f"  [{_code}] typos={len(_tb.common_typos)} "
            f"adjacent={len(_tb.keyboard_adjacent)} "
            f"homophones={len(_tb.homophone_groups)} "
            f"confused={len(_tb.confused_words)} "
            f"slang={len(_tb.slang_whitelist)}")
    logger.info(f"  Excl. processes: {len(EXCLUDED_PROCESSES)}")

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

    load_config()
    load_personal_data()
    _start_lazy_language_loads()   # fr/de/es download in background; tray
    _start_model_loading()         # shows them as "(loading…)" meanwhile
    threading.Thread(target=_layout_watcher, daemon=True).start()
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
        CYCLE_LANGUAGE:     cycle_language,
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
