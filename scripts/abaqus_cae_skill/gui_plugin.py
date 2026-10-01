"""Auto-starting GUI bridge for abaqus-cae-skill (derived from Abaqus-Control-MCP, MIT).

This file is packaged so the installer can copy it into the Abaqus plugin
search directory without requiring a source checkout.
"""

from abaqusGui import (
    AFXForm,
    AFXMode,
    FXMAPFUNC,
    SEL_COMMAND,
    SEL_TIMEOUT,
    addExitCallback,
    getAFXApp,
    sendCommand,
    showAFXErrorDialog,
)
import base64
import json
import os
import platform
import queue
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import uuid

# __ABAQUS_CAE_INSTALL_CONFIG__
if '_INSTALL_CONFIG' not in globals():
    raise RuntimeError('Install this plugin with abaqus-cae install before loading it')
HOST = '127.0.0.1'
PORT = int(_INSTALL_CONFIG['preferredPort'])
SESSION_ID = uuid.uuid4().hex
SESSION_PATH = os.path.join(_INSTALL_CONFIG['configDirectory'], 'sessions', SESSION_ID + '.json')
LOG_PATH = os.path.join(_INSTALL_CONFIG['configDirectory'], 'bridge-' + SESSION_ID + '.log')
_DEBUG_LOG = os.environ.get('ABAQUS_CAE_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')
_LOG_LIMIT = 1024 * 1024
_LOG_LOCK = threading.Lock()


def _log(message):
    if not _DEBUG_LOG:
        return
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= _LOG_LIMIT:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _clear_old_log():
    if _DEBUG_LOG:
        return
    for path in (LOG_PATH, LOG_PATH + ".1"):
        try:
            os.remove(path)
        except OSError:
            pass


def _announce(message):
    print(message)
    try:
        main_window = getAFXApp().getAFXMainWindow()
        if hasattr(main_window, "writeToMessageArea"):
            main_window.writeToMessageArea(message)
    except Exception:
        pass


def _kernel_wrapper(code, response_path, execution_id="", source_filename=""):
    encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
    encoded_path = base64.b64encode(response_path.encode("utf-8")).decode("ascii")
    encoded_id = base64.b64encode(execution_id.encode("ascii")).decode("ascii")
    encoded_filename = base64.b64encode(source_filename.encode("utf-8")).decode("ascii")
    template = r'''
import ast
import base64
import builtins
import contextlib
import difflib
import io
import inspect
import json
import linecache
import os
import re
import sys
import traceback

code = base64.b64decode("__ABAQUS_CAE_CODE__").decode("utf-8")
response_path = base64.b64decode("__ABAQUS_CAE_RESPONSE__").decode("utf-8")
execution_id = base64.b64decode("__ABAQUS_CAE_ID__").decode("ascii")
source_filename = base64.b64decode("__ABAQUS_CAE_FILENAME__").decode("utf-8")

def _jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except Exception:
        try:
            description = repr(value)
        except Exception as exc:
            description = "<repr failed: %s>" % type(exc).__name__
        return {
            "repr": description[:4000],
            "type": "%s.%s" % (type(value).__module__, type(value).__name__),
        }

def _node_source(node):
    try:
        return ast.unparse(node)
    except Exception:
        return None

def _key_literal(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Index):
        return _key_literal(node.value)
    return None

def _extract_tb_lineno(exc):
    if hasattr(exc, "lineno") and getattr(exc, "lineno") is not None:
        return getattr(exc, "lineno")
    tb = exc.__traceback__
    lineno = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename.startswith("<abaqus-cae:"):
            lineno = tb.tb_lineno
        tb = tb.tb_next
    return lineno

def _find_subscript_parent(source, missing_key, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _key_literal(node.slice) == missing_key:
            src = _node_source(node.value)
            if src is not None:
                candidates.append((getattr(node, "lineno", 0), src))
    # Fallback: if no candidates and lineno is provided, match any Subscript on the failed line
    if not candidates and lineno is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                node_lineno = getattr(node, "lineno", None)
                if node_lineno == lineno:
                    src = _node_source(node.value)
                    if src is not None:
                        candidates.append((node_lineno, src))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda c: abs(c[0] - lineno))
    return candidates[0][1]

def _find_attribute_parent(source, missing_attr, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == missing_attr:
            candidates.append((getattr(node, "lineno", 0), _node_source(node.value)))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda item: abs(item[0] - lineno))
    return candidates[0][1]

def _extract_func_name(exc):
    msg = str(exc)
    m = re.search(r"([\w.]+)\(\)", msg)
    if m:
        return m.group(1)
    m = re.search(r"'([\w.]+)'", msg)
    if m:
        return m.group(1)
    return None


def _extract_code_excerpt(code, lineno, radius=2):
    if lineno is None:
        return None
    lines = code.splitlines()
    if not lines:
        return None
    index = max(0, lineno - 1)
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    excerpt_lines = []
    for i in range(start, end):
        prefix = ">>" if i == index else "  "
        excerpt_lines.append("%s %4d | %s" % (prefix, i + 1, lines[i]))
    return "\n".join(excerpt_lines)

def _resolve_simple_expr(expr, namespace):
    def _eval_node(node):
        if isinstance(node, ast.Name):
            if node.id in namespace:
                return namespace[node.id]
            return getattr(builtins, node.id)
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = _eval_node(node.value)
            try:
                key = ast.literal_eval(node.slice)
            except Exception:
                if isinstance(node.slice, ast.Name):
                    key = namespace[node.slice.id]
                elif isinstance(node.slice, ast.Index):
                    if isinstance(node.slice.value, ast.Constant):
                        key = node.slice.value.value
                    elif isinstance(node.slice.value, ast.Name):
                        key = namespace[node.slice.value.id]
                    else:
                        raise
                else:
                    raise
            return base[key]
        raise ValueError("unsupported expression node")

    parsed = ast.parse(expr, mode="eval")
    return _eval_node(parsed.body)

def _extract_params_from_sig(sig_str):
    m = re.search(r"\((.*)\)", sig_str)
    if not m:
        return []
    content = m.group(1)
    params = []
    paren_depth = 0
    current_param = []
    for char in content:
        if char in "([{":
            paren_depth += 1
            current_param.append(char)
        elif char in ")]}":
            paren_depth -= 1
            current_param.append(char)
        elif char == "," and paren_depth == 0:
            params.append("".join(current_param).strip())
            current_param = []
        else:
            current_param.append(char)
    if current_param:
        params.append("".join(current_param).strip())

    names = []
    for p in params:
        if not p:
            continue
        word = re.match(r"^([a-zA-Z_]\w*)", p)
        if word:
            name = word.group(1)
            if name not in ("self", "args", "kwargs"):
                names.append(name)
    return names


def _extract_invalid_keyword(msg):
    patterns = (
        r"got an unexpected keyword argument ['\"](\w+)['\"]",
        r"['\"](\w+)['\"] is an invalid keyword argument",
        r"keyword error on (\w+)",
    )
    for pattern in patterns:
        match = re.search(pattern, msg, re.I)
        if match:
            return match.group(1)
    return None


def _extract_call_target(code, lineno):
    if lineno is None:
        return None
    try:
        tree = ast.parse(code)
    except Exception:
        try:
            lines = code.splitlines()
            if lineno - 1 < 0 or lineno - 1 >= len(lines):
                return None
            tree = ast.parse(lines[lineno - 1], mode="exec")
        except Exception:
            return None

    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None:
                if start <= lineno <= end:
                    candidates.append(node)
            elif start == lineno:
                candidates.append(node)

    if not candidates:
        return None

    candidates.sort(key=lambda n: getattr(n, "end_lineno", getattr(n, "lineno", 0)) - getattr(n, "lineno", 0))
    return _node_source(candidates[0].func)

def _summarize_mapping_keys(mapping_obj, missing_key):
    result = {"possible_keys": []}
    keys_method = getattr(mapping_obj, "keys", None)
    if not callable(keys_method):
        return result
    try:
        key_texts = [str(k) for k in list(keys_method())]
    except Exception:
        return result

    result["available_key_count"] = len(key_texts)
    if missing_key is not None:
        try:
            result["possible_keys"] = difflib.get_close_matches(str(missing_key), key_texts, n=3, cutoff=0.6)
        except Exception:
            pass
    if not result["possible_keys"]:
        result["available_keys_sample"] = key_texts[:5]
    return result

def _summarize_object_members(obj, missing_attr):
    result = {"possible_members": []}
    try:
        members = sorted([name for name in dir(obj) if not name.startswith("_")])
    except Exception:
        return result

    if missing_attr:
        try:
            result["possible_members"] = difflib.get_close_matches(missing_attr, members, n=3, cutoff=0.6)
        except Exception:
            pass
    return result

def _extract_signature_from_docstring(doc, target_name):
    pattern = r"\b" + re.escape(target_name) + r"\s*\("
    match = re.search(pattern, doc)
    if not match:
        return None
    start_pos = match.start()
    open_paren_idx = match.end() - 1
    paren_count = 0
    end_pos = -1
    for i in range(open_paren_idx, min(open_paren_idx + 1000, len(doc))):
        char = doc[i]
        if char == "(":
            paren_count += 1
        elif char == ")":
            paren_count -= 1
            if paren_count == 0:
                end_pos = i + 1
                break
    if end_pos != -1:
        sig_candidate = doc[start_pos:end_pos]
        return " ".join(sig_candidate.split())
    return None


def _summarize_callable(target_expr, namespace, invalid_keyword=None):
    summary = {
        "call_target": target_expr,
        "callable_signature": None,
        "callable_summary": None,
    }
    if not target_expr:
        return summary
    try:
        target = _resolve_simple_expr(target_expr, namespace)
    except Exception:
        return summary

    target_name = getattr(target, "__name__", "function")
    try:
        sig = inspect.signature(target)
        sig_str = "%s%s" % (target_name, sig)
    except Exception:
        sig_str = "%s(...)" % target_name

    doc = None
    try:
        doc = inspect.getdoc(target)
    except Exception:
        pass
    if not doc:
        try:
            doc = getattr(target, "__doc__", None)
        except Exception:
            pass

    if doc:
        try:
            lines = doc.strip().splitlines()
            if lines:
                first_line = lines[0].strip()
                if " -> " in first_line:
                    first_line = first_line.split(" -> ", 1)[1].strip()
                summary["callable_summary"] = first_line if first_line else None
            if sig_str.endswith("(...)"):
                extracted_sig = _extract_signature_from_docstring(doc, target_name)
                if extracted_sig:
                    sig_str = extracted_sig
        except Exception:
            pass

    if len(sig_str) > 320:
        sig_str = sig_str[:317] + "..."
    summary["callable_signature"] = sig_str

    if invalid_keyword:
        try:
            valid_params = []
            try:
                sig = inspect.signature(target)
                valid_params = [p.name for p in sig.parameters.values() if p.name not in ("self", "args", "kwargs")]
            except Exception:
                pass
            if not valid_params:
                valid_params = _extract_params_from_sig(sig_str)
            if valid_params:
                matches = difflib.get_close_matches(invalid_keyword, valid_params, n=3, cutoff=0.6)
                if matches:
                    summary["possible_keywords"] = matches
        except Exception:
            pass

    return summary

def _is_user_source(filename):
    if filename.startswith("<abaqus-cae:"):
        return True
    if not filename or filename.startswith("<") or not os.path.isfile(filename):
        return False
    normalized = os.path.normcase(os.path.abspath(filename))
    portable = normalized.replace("\\", "/").lower()
    if "/site-packages/" in portable or "/dist-packages/" in portable:
        return False
    for root in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.dirname(traceback.__file__))):
        try:
            root_path = os.path.normcase(os.path.abspath(root))
            if os.path.commonpath((normalized, root_path)) == root_path:
                return False
        except (OSError, ValueError):
            pass
    return True


def _format_execution_error(source, exc, namespace=None):
    tb_str = traceback.format_exc()
    tb_lines = [l for l in tb_str.strip().splitlines() if l.strip()]
    core_error = tb_lines[-1] if tb_lines else str(exc)
    exc_type_name = type(exc).__name__
    error_module = type(exc).__module__
    if not isinstance(error_module, str):
        error_module = getattr(error_module, "__name__", str(error_module))
    error_type = "%s.%s" % (error_module, exc_type_name)
    tb = exc.__traceback__
    user_frames = []
    while tb is not None:
        if _is_user_source(tb.tb_frame.f_code.co_filename):
            user_frames.append(tb)
        tb = tb.tb_next
    primary = user_frames[-1] if user_frames else None
    syntax_file = getattr(exc, "filename", None) if isinstance(exc, SyntaxError) else None
    if syntax_file and _is_user_source(syntax_file):
        lineno = getattr(exc, "lineno", None)
        primary_frame = {"file": syntax_file, "line": lineno, "function": "<module>"}
    elif primary:
        lineno = primary.tb_lineno
        primary_frame = {"file": primary.tb_frame.f_code.co_filename,
                         "line": lineno, "function": primary.tb_frame.f_code.co_name}
    else:
        lineno = _extract_tb_lineno(exc)
        primary_frame = None
    source_file = primary_frame.get("file") if primary_frame else None
    if source_file and not source_file.startswith("<"):
        linecache.checkcache(source_file)
    source_for_error = ''.join(linecache.getlines(source_file)) if source_file else source
    if not source_for_error:
        source_for_error = source
    diagnostic_namespace = dict(namespace or {})
    if primary is not None:
        diagnostic_namespace.update(primary.tb_frame.f_globals)
        diagnostic_namespace.update(primary.tb_frame.f_locals)
    code_excerpt = _extract_code_excerpt(source_for_error, lineno)
    source_lines = source_for_error.splitlines()
    code_line = (source_lines[lineno - 1].strip()[:240]
                 if lineno is not None and 0 < lineno <= len(source_lines) else None)

    is_key_error = isinstance(exc, KeyError) or exc_type_name.endswith("KeyError")
    is_attribute_error = isinstance(exc, AttributeError) or exc_type_name.endswith("AttributeError")
    is_name_error = exc_type_name == "NameError"
    is_type_error = isinstance(exc, TypeError) or exc_type_name.endswith("TypeError")
    is_syntax_error = isinstance(exc, SyntaxError) or exc_type_name.endswith("SyntaxError")

    detected_missing_key = None
    detected_parent_path = None
    if lineno is not None:
        try:
            lines = source_for_error.splitlines()
            if 0 <= lineno - 1 < len(lines):
                line_code = lines[lineno - 1]
                tree = ast.parse(line_code)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Subscript):
                        val_key = None
                        if isinstance(node.slice, ast.Constant):
                            val_key = node.slice.value
                        elif isinstance(node.slice, ast.Name):
                            val_key = diagnostic_namespace.get(node.slice.id)
                        elif isinstance(node.slice, ast.Index):
                            if isinstance(node.slice.value, ast.Constant):
                                val_key = node.slice.value.value
                            elif isinstance(node.slice.value, ast.Name):
                                val_key = diagnostic_namespace.get(node.slice.value.id)

                        if val_key is not None:
                            p_path = _node_source(node.value)
                            if p_path:
                                try:
                                    p_obj = _resolve_simple_expr(p_path, diagnostic_namespace)
                                    keys_method = getattr(p_obj, "keys", None)
                                    if callable(keys_method):
                                        try:
                                            try:
                                                keys_list = list(keys_method())
                                                is_missing = val_key not in keys_list
                                            except Exception:
                                                is_missing = val_key not in p_obj
                                            if is_missing:
                                                is_key_error = True
                                                detected_missing_key = val_key
                                                detected_parent_path = p_path
                                                break
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
        except Exception:
            pass

    if is_key_error:
        missing_key = detected_missing_key if detected_missing_key is not None else (exc.args[0] if exc.args else None)
        parent_path = detected_parent_path if detected_parent_path is not None else _find_subscript_parent(source_for_error, missing_key, lineno)
        recovery = {
            "missing_key": _jsonable(missing_key),
            "parent_object_path": parent_path,
        }
        if parent_path:
            try:
                parent_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
                recovery.update(_summarize_mapping_keys(parent_obj, missing_key))
            except Exception:
                pass
    elif is_attribute_error:
        missing_attr = getattr(exc, "name", None)
        source_obj = getattr(exc, "obj", None)
        if not missing_attr:
            match = re.search(r"has no attribute ['\"](\w+)['\"]", str(exc))
            if match:
                missing_attr = match.group(1)
        parent_path = _find_attribute_parent(source_for_error, missing_attr, lineno) if missing_attr else None
        if source_obj is None and parent_path:
            try:
                source_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
            except Exception:
                pass
        object_type = type(source_obj).__name__ if source_obj is not None else None
        recovery = {
            "missing_attribute": missing_attr,
            "object_type": object_type,
            "parent_object_path": parent_path,
        }
        if source_obj is not None:
            try:
                recovery.update(_summarize_object_members(source_obj, missing_attr))
            except Exception:
                pass
    elif is_name_error:
        missing_var = getattr(exc, "name", None)
        if not missing_var:
            m = re.search(r"name '(\w+)' is not defined", str(exc))
            if m:
                missing_var = m.group(1)
        recovery = {"missing_variable": missing_var}
        if missing_var:
            names = set(str(key) for key in diagnostic_namespace if isinstance(key, str))
            names.update(dir(builtins))
            matches = difflib.get_close_matches(missing_var, sorted(names), n=3, cutoff=0.6)
            if matches:
                recovery["possible_names"] = matches
        abaqus_modules = {
            "mesh", "part", "material", "assembly", "step", "interaction",
            "load", "section", "sketch", "job", "connector", "visualization",
            "xyPlot", "displayGroup", "meshEdit", "connectorBehavior", "symbolicConstants"
        }
        if missing_var in abaqus_modules:
            recovery["import_suggestion"] = "from abaqus import %s" % missing_var
        elif missing_var == "C":
            recovery["import_suggestion"] = "from abaqusConstants import *"
        elif missing_var and missing_var.isupper() and len(missing_var) > 1:
            recovery["import_suggestion"] = "from abaqusConstants import *"
    elif is_syntax_error:
        recovery = {
            "syntax_line": getattr(exc, "lineno", None),
            "syntax_offset": getattr(exc, "offset", None),
            "syntax_text": getattr(exc, "text", None),
        }
    elif is_type_error:
        call_target = _extract_call_target(source_for_error, lineno)
        invalid_kw = _extract_invalid_keyword(core_error)
        recovery = {"call_target": call_target}
        recovery.update(_summarize_callable(call_target, diagnostic_namespace, invalid_kw))
    else:
        recovery = {}

    return {
        "ok": False,
        "core_error": core_error,
        "message": getattr(exc, "msg", str(exc)) if isinstance(exc, SyntaxError) else str(exc),
        "error_type": error_type,
        "error_line": lineno,
        "primary_frame": primary_frame,
        "traceback": tb_str,
        "frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ],
        "code_excerpt": code_excerpt,
        "code_line": code_line,
        "recovery": recovery,
    }

_MAX_OUTPUT = 4000

class _LimitedWriter:
    encoding = "utf-8"

    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.count = 0

    def write(self, value):
        length = len(value)
        if self.count < self.limit:
            self.parts.append(value[:self.limit - self.count])
        self.count += length
        return length

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        result = "".join(self.parts)
        if self.count > self.limit:
            result += "\n... (truncated, total %d chars)" % self.count
        return result

_mdb_obj = globals().get("mdb")
_session_obj = globals().get("session")
if _mdb_obj is None:
    try:
        from abaqus import mdb as _mdb_obj
    except Exception:
        pass
if _session_obj is None:
    try:
        from abaqus import session as _session_obj
    except Exception:
        pass

namespace = globals().setdefault("_ABAQUS_CAE_GLOBALS", {
    "__name__": "__abaqus_cae_exec__",
    "__doc__": None,
})
namespace.update({
    "mdb": _mdb_obj,
    "session": _session_obj,
})
namespace.pop("result", None)

filename = source_filename or "<abaqus-cae:%s>" % (execution_id or "legacy")
if len(code) <= 262144:
    keys = globals().setdefault("_ABAQUS_CAE_SOURCE_KEYS", [])
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    keys.append(filename)
    if len(keys) > 128:
        linecache.cache.pop(keys.pop(0), None)

stdout = _LimitedWriter(_MAX_OUTPUT)
stderr = _LimitedWriter(_MAX_OUTPUT)
returned = None
error_response = None

_missing = object()
_previous_file = namespace.get('__file__', _missing)
_previous_name = namespace.get('__name__', _missing)
_previous_path = list(sys.path)
if source_filename:
    namespace['__file__'] = source_filename
    namespace['__name__'] = '__main__'
    sys.path.insert(0, os.path.dirname(source_filename))
try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            parsed = ast.parse(code, filename=filename, mode="exec")
            if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr):
                expression = ast.Expression(body=parsed.body[0].value)
                compiled = compile(expression, filename, "eval")
                returned = eval(compiled, namespace, namespace)
            else:
                compiled = compile(parsed, filename, "exec")
                exec(compiled, namespace, namespace)
                returned = namespace.pop("result", None)
        except Exception as exc:
            error_response = _format_execution_error(code, exc, namespace)
except Exception as exc:
    error_response = _format_execution_error(code, exc)
finally:
    namespace.pop("result", None)
    if source_filename:
        sys.path[:] = _previous_path
        for _key, _previous in (('__file__', _previous_file), ('__name__', _previous_name)):
            if _previous is _missing:
                namespace.pop(_key, None)
            else:
                namespace[_key] = _previous

captured_stdout = stdout.getvalue()
captured_stderr = stderr.getvalue()
if error_response is not None:
    error_response["executionId"] = execution_id
    error_response["stdout"] = captured_stdout
    error_response["stderr"] = captured_stderr
    payload = {"ok": True, "result": error_response}
else:
    payload = {
        "ok": True,
        "result": {
            "ok": True,
            "executionId": execution_id,
            "return_value": _jsonable(returned),
            "stdout": captured_stdout,
            "stderr": captured_stderr,
        },
    }

temporary_path = response_path + ".tmp"
with open(temporary_path, "w") as handle:
    json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
os.replace(temporary_path, response_path)
'''
    return (
        template.replace("__ABAQUS_CAE_CODE__", encoded_code)
        .replace("__ABAQUS_CAE_RESPONSE__", encoded_path)
        .replace("__ABAQUS_CAE_ID__", encoded_id)
        .replace("__ABAQUS_CAE_FILENAME__", encoded_filename)
    )


def _run_kernel_code(code, timeout, execution_id="", source_filename=""):
    response_path = os.path.join(
        tempfile.gettempdir(), "abaqus_cae_%s.json" % uuid.uuid4().hex
    )
    command = _kernel_wrapper(code, response_path, execution_id, source_filename)
    _log("sendCommand start response_path=%s" % response_path)
    sendCommand(command, False, False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Keep discovery and request buffering responsive while the kernel works;
        # queued model commands are still executed serially by the dispatcher.
        if _SERVER is not None:
            _SERVER.poll_requests()
        if os.path.exists(response_path):
            with open(response_path, "r") as handle:
                payload = json.load(handle)
            try:
                os.remove(response_path)
            except Exception:
                pass
            if not payload.get("ok"):
                error = payload.get("error", {})
                _log("kernel error type=%s" % error.get("type", "unknown"))
                raise RuntimeError(error.get("message", "kernel command failed"))
            _log("kernel response ok")
            result = payload["result"]
            if result.get("executionId") != execution_id:
                raise RuntimeError("kernel response executionId mismatch")
            return result
        time.sleep(0.05)

    raise TimeoutError("timed out waiting for Abaqus kernel response")


class SessionMismatchError(RuntimeError):
    pass


class McpGuiHandler:
    """One nonblocking connection, advanced only by the GUI event loop."""

    def __init__(self, request):
        self.request = request
        self.request.setblocking(False)
        self.input = bytearray()
        self.output = b''
        self.received = False
        self.request_id = None
        self.item = None
        self.deadline = time.monotonic() + 5.0

    def reply(self, payload):
        self.received = True
        self.item = None
        self.output = json.dumps(payload, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8') + b'\n'
        self.deadline = time.monotonic() + 5.0

    def error(self, exc):
        self.reply({'id': self.request_id, 'ok': False, 'error': {
            'message': str(exc), 'type': '%s.%s' % (type(exc).__module__, type(exc).__name__),
            'traceback': traceback.format_exc(),
            'state': 'NOT_STARTED' if isinstance(exc, SessionMismatchError) else (
                'CANCELLED' if 'request cancelled' in str(exc) else 'UNKNOWN'),
        }})

    def dispatch(self, message):
        self.request_id = message.get('id')
        method = message.get('method')
        params = message.get('params') or {}
        if params.get('sessionId') and params['sessionId'] != SESSION_ID:
            raise SessionMismatchError('Target CAE session has changed; execution not started')
        if method == 'describe':
            self.reply({'id': self.request_id, 'ok': True, 'result': dict(_SESSION_INFO)})
            return
        if _EXITING or _SERVER is None:
            raise SessionMismatchError('CAE bridge is closing; execution not started')
        self.deadline = time.monotonic() + float(params.get('timeout') or 60) + 5.0
        self.item = GuiRequest(method, params, deadline=self.deadline)
        _REQUESTS.put(self.item)

    def poll(self):
        try:
            if not self.received:
                if time.monotonic() >= self.deadline:
                    return False
                # Bound per-poll reads; incomplete messages must not freeze CAE.
                for _ in range(16):
                    try:
                        chunk = self.request.recv(4096)
                    except BlockingIOError:
                        break
                    if not chunk:
                        return False
                    self.input.extend(chunk)
                    if len(self.input) > 16 * 1024 * 1024:
                        raise ValueError('request exceeded 16777216 bytes')
                    newline = self.input.find(b'\n')
                    if newline >= 0:
                        self.received = True
                        self.dispatch(json.loads(self.input[:newline].decode('utf-8')))
                        break
            if self.item is not None:
                if self.item.event.is_set():
                    if self.item.error is not None:
                        raise self.item.error
                    self.reply({'id': self.request_id, 'ok': True, 'result': self.item.result})
                elif time.monotonic() >= self.deadline:
                    if self.item.cancel_if_queued():
                        raise TimeoutError('timed out before GUI execution; request cancelled')
                    raise TimeoutError('timed out while GUI execution was running; outcome unknown')
            if self.output:
                try:
                    sent = self.request.send(self.output)
                    if not sent:
                        return False
                    self.output = self.output[sent:]
                    return bool(self.output)
                except BlockingIOError:
                    pass
            return time.monotonic() < self.deadline
        except OSError:
            return False
        except Exception as exc:
            self.error(exc)
            return True

    def close(self):
        if self.item is not None and self.item.cancel_if_queued():
            self.item.error = RuntimeError('connection closed before GUI execution; request cancelled')
            self.item.event.set()
        self.request.close()


class McpGuiServer(socketserver.TCPServer):
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        socketserver.TCPServer.__init__(self, *args, **kwargs)
        self.socket.setblocking(False)
        self.connections = []

    def poll_requests(self):
        for _ in range(16):
            try:
                request, address = self.socket.accept()
            except BlockingIOError:
                break
            self.connections.append(self.RequestHandlerClass(request))
        for connection in self.connections[:]:
            if not connection.poll():
                connection.close()
                self.connections.remove(connection)

    def request_stop(self):
        # No Python worker threads survive into CAE's embedded interpreter exit.
        for connection in self.connections:
            connection.close()
        self.connections[:] = []
        socketserver.TCPServer.server_close(self)


_SERVER = None
_DISPATCHER = None
_REQUESTS = queue.Queue()
_SESSION_INFO = {}
_EXITING = False


class GuiRequest:
    def __init__(self, method, params, deadline=None):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = "queued"
        self.result = None
        self.error = None
        self.deadline = deadline

    def cancel_if_queued(self):
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            return True

    def start_if_queued(self):
        with self.lock:
            if self.state != "queued":
                return False
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self.state = 'cancelled'
                self.error = TimeoutError('timed out before GUI execution; request cancelled')
                self.event.set()
                return False
            self.state = "running"
            return True


def _handle_on_gui_thread(item):
    method = item.method
    params = item.params
    timeout = float(params.get("timeout") or os.environ.get("ABAQUS_CAE_TIMEOUT", "60"))
    execution_id = params.get("executionId", "")

    if method == "ping":
        code = (
            "import os, sys, platform, abaqus\n"
            "from abaqus import mdb, session\n"
            "result = {'python': sys.version, 'executable': sys.executable, "
            "'platform': platform.platform(), 'pid': os.getpid(), "
            "'cpu_count': os.cpu_count(), "
            "'abaqus_version': str(abaqus.version) if hasattr(abaqus, 'version') else None, "
            "'workingDirectory': os.getcwd(), "
            "'models': list(mdb.models.keys()), "
            "'viewports': list(session.viewports.keys())}"
        )
        result = _run_kernel_code(code, timeout, execution_id)
        result = result["return_value"]
        result["guiProcess"] = {
            "python": sys.version,
            "platform": platform.platform(),
            "thread": threading.current_thread().name,
        }
        result['sessionId'] = SESSION_ID
        result['installationId'] = _INSTALL_CONFIG['installationId']
        result['port'] = PORT
        return result

    if method == "execute":
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("params.code must be a non-empty string")
        return _run_kernel_code(code, timeout, execution_id, params.get("sourceFilename", ""))

    raise ValueError("unknown method: %r" % method)


def _publish_session():
    directory = os.path.dirname(SESSION_PATH)
    os.makedirs(directory, exist_ok=True)
    _SESSION_INFO['updatedAt'] = time.time()
    temporary = SESSION_PATH + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(_SESSION_INFO, handle, ensure_ascii=False)
    os.replace(temporary, SESSION_PATH)


def start_gui_agent():
    global _SERVER, PORT
    if _EXITING or _SERVER is not None:
        return
    try:
        _SERVER = McpGuiServer((HOST, PORT), McpGuiHandler)
    except OSError:
        if PORT == 0:
            raise
        _SERVER = McpGuiServer((HOST, 0), McpGuiHandler)
    PORT = _SERVER.server_address[1]
    _SESSION_INFO.update({'schema': 1, 'sessionId': SESSION_ID,
        'installationId': _INSTALL_CONFIG['installationId'],
        'host': HOST, 'port': PORT, 'guiPid': os.getpid(),
        'pluginPath': _INSTALL_CONFIG['pluginPath'], 'version': _INSTALL_CONFIG['version'],
        'startedAt': time.time()})
    _publish_session()
    _announce('Abaqus CAE bridge ready: %s:%s (session %s)' % (HOST, PORT, SESSION_ID[:8]))


def stop_gui_agent(cancel_pending=True):
    global _SERVER
    server, _SERVER = _SERVER, None
    if server is not None:
        server.request_stop()
    if cancel_pending:
        while True:
            try:
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            if item.cancel_if_queued():
                item.error = RuntimeError('bridge stopped before GUI execution; request cancelled')
                item.event.set()
    try:
        os.remove(SESSION_PATH)
    except OSError:
        pass


def _on_cae_exit():
    global _EXITING
    _EXITING = True
    if _DISPATCHER is not None:
        _DISPATCHER.cancel_timers()
    stop_gui_agent()


class CaeBridgeDispatcher(AFXForm):
    ID_BOOT = AFXForm.ID_LAST + 1
    ID_POLL = AFXForm.ID_LAST + 2

    def __init__(self, owner):
        AFXForm.__init__(self, owner)
        self.poll_pending = False
        self.boot_attempts = 0
        self.last_publish = 0
        self.boot_timer = None
        self.poll_timer = None
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_BOOT, CaeBridgeDispatcher.onBoot)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, CaeBridgeDispatcher.onPoll)
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, CaeBridgeDispatcher.onActivate)

    def getFirstDialog(self):
        return None

    def onActivate(self, sender, sel, ptr):
        # Optional restart menu. Normal startup uses the boot timer.
        return self.onBoot(sender, sel, ptr)

    def onBoot(self, sender, sel, ptr):
        self.boot_timer = None
        if _EXITING:
            return 1
        self.boot_attempts += 1
        try:
            start_gui_agent()
            self.schedule_poll()
        except Exception as exc:
            _log('Automatic bridge startup failed: %s' % exc)
            if self.boot_attempts < 5:
                self.boot_timer = getAFXApp().addTimeout(500, self, self.ID_BOOT)
            else:
                _announce('Abaqus CAE bridge could not start: %s' % exc)
        return 1

    def schedule_poll(self):
        if not _EXITING and not self.poll_pending and _SERVER is not None:
            self.poll_pending = True
            self.poll_timer = getAFXApp().addTimeout(100, self, self.ID_POLL)

    def cancel_timers(self):
        for timer in (self.boot_timer, self.poll_timer):
            if timer is not None:
                getAFXApp().removeTimeout(timer)
        self.boot_timer = self.poll_timer = None
        self.poll_pending = False

    def onPoll(self, sender, sel, ptr):
        self.poll_pending = False
        self.poll_timer = None
        if _EXITING or _SERVER is None:
            return 1
        _SERVER.poll_requests()
        for _ in range(5):
            try:
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            if not item.start_if_queued():
                continue
            try:
                item.result = _handle_on_gui_thread(item)
            except Exception as exc:
                item.error = exc
            finally:
                with item.lock:
                    item.state = 'done'
                item.event.set()
        if _SERVER is not None:
            _SERVER.poll_requests()
        if _SERVER is not None and time.time() - self.last_publish >= 10:
            try:
                _publish_session()
                self.last_publish = time.time()
            except OSError as exc:
                _log('Session publication failed: %s' % exc)
        self.schedule_poll()
        return 1


# Plugin import runs in the GUI process. Defer service startup to its event loop;
# no menu click and no abaqus_v6.env modification are needed.
toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
_DISPATCHER = CaeBridgeDispatcher(toolset)
toolset.registerGuiMenuButton(object=_DISPATCHER, buttonText='Abaqus CAE Skill|Restart Bridge',
    version=_INSTALL_CONFIG['version'], applicableModules=['Part', 'Property', 'Assembly', 'Step',
        'Interaction', 'Load', 'Mesh', 'Job', 'Visualization', 'Sketch'],
    description='Restart the bridge if its automatic startup failed.')
_DISPATCHER.boot_timer = getAFXApp().addTimeout(250, _DISPATCHER, _DISPATCHER.ID_BOOT)
addExitCallback(_on_cae_exit)

# CAE owns its embedded Python shutdown. Python atexit callbacks run too late
# here and can hang or crash the GUI; use only the Abaqus exit callback above.
