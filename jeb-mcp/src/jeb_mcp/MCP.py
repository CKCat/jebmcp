# -*- coding: utf-8 -*-

import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
import hashlib
import xml.etree.ElementTree as ET

try:
    import BaseHTTPServer
    import SocketServer
    from urlparse import urlparse
except ImportError:
    import http.server as BaseHTTPServer
    import socketserver as SocketServer
    from urllib.parse import urlparse

try:
    from jarray import zeros
except ImportError:
    def zeros(length, type_char):
        return bytearray(length)

try:
    import java.lang.Throwable as JavaThrowable
    from java.io import File, FileOutputStream
    from java.lang import System as JavaSystem
except Exception:
    class JavaThrowable(Exception):
        pass
    JavaSystem = None
    File = None
    FileOutputStream = None

try:
    from com.pnfsoftware.jeb.client.api import IScript
    from com.pnfsoftware.jeb.core import Artifact, RuntimeProjectUtil
    from com.pnfsoftware.jeb.core.actions import (
        ActionContext,
        ActionCommentData,
        ActionOverridesData,
        ActionRenameData,
        Actions,
        ActionXrefsData,
    )
    from com.pnfsoftware.jeb.core.input import FileInput
    from com.pnfsoftware.jeb.core.output.text import TextDocumentUtil
    from com.pnfsoftware.jeb.core.units.code.android import IApkUnit
    from com.pnfsoftware.jeb.core.util import DecompilerHelper
except Exception:
    IScript = object
    Artifact = None
    RuntimeProjectUtil = None
    ActionContext = None
    ActionCommentData = None
    ActionOverridesData = None
    ActionRenameData = None
    Actions = None
    ActionXrefsData = None
    FileInput = None
    TextDocumentUtil = None
    IApkUnit = None
    DecompilerHelper = None


try:
    _unicode_type = unicode
except NameError:
    _unicode_type = str

try:
    _bytes_type = bytes
except NameError:
    _bytes_type = str


def to_unicode_safe(val):
    """
    Python 2/3 安全的 Unicode 转换函数，杜绝与 str 隐式拼接时抛出 UnicodeDecodeError。
    """
    if val is None:
        return u""
    if isinstance(val, _unicode_type):
        return val
    if isinstance(val, str):
        try:
            return val.decode("utf-8")
        except Exception:
            try:
                return val.decode("utf-8", "ignore")
            except Exception:
                return _unicode_type(val)
    if isinstance(val, _bytes_type):
        try:
            return val.decode("utf-8", "ignore")
        except Exception:
            return str(val)
    try:
        return _unicode_type(val)
    except Exception:
        return u""


# Python 2.7 doesn't have typing, so we define minimal substitutes
class Any(object):
    pass


def get_type_hints(func):
    """
    Mock for get_type_hints for Python 2.7 / Jython compatibility.
    Extracts positional arg names via inspect.getargspec.
    """
    hints = {}

    if hasattr(func, "__annotations__"):
        hints.update(getattr(func, "__annotations__", {}))

    try:
        args, varargs, keywords, defaults = inspect.getargspec(func)
    except Exception:
        return hints

    for arg in args:
        if arg == "self":
            continue
        if arg not in hints:
            hints[arg] = Any

    return hints


class ErrorMessages:
    SUCCESS = "[Success]"
    MISSING_PARAM = "[Error] Missing parameter."
    LOAD_APK_FAILED = "[Error] Load apk failed."
    LOAD_APK_NOT_FOUND = "[Error] Apk file not found."
    GET_MANIFEST_FAILED = "[Error] Get AndroidManifest text failed."
    INDEX_OUT_OF_BOUNDS = "[Error] Index out of bounds."
    DECOMPILE_FAILED = "[Error] Failed to decompile code."
    METHOD_NOT_FOUND = "[Error] Method not found in current apk, use check_java_identifier tool check your input first."
    METHOD_NOT_FOUND_WITHOUT_CHECK = "[Error] Method not found in current apk."
    CLASS_NOT_FOUND = "[Error] Class not found in current apk, use check_java_identifier tool check your input first."
    CLASS_NOT_FOUND_WITHOUT_CHECK = "[Error] Class not found in current apk."
    FIELD_NOT_FOUND = "[Error] Field not found in current apk, use check_java_identifier tool check your input first."
    FIELD_NOT_FOUND_WITHOUT_CHECK = "[Error] Field not found in current apk."
    RESOURCE_NOT_FOUND = "[Error] Resource not found."
    ADDRESS_NOT_FOUND = "[Error] Address not found in code unit."
    VAR_NOT_FOUND = "[Error] Variable not found in pseudo-code."


class JSONRPCError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = to_unicode_safe(message)
        self.data = data


class RPCRegistry(object):
    def __init__(self):
        self.methods = {}

    def register(self, func):
        self.methods[func.__name__] = func
        return func

    def dispatch(self, method, params):
        if method not in self.methods:
            raise JSONRPCError(-32601, u"Method '{0}' not found".format(to_unicode_safe(method)))

        func = self.methods[method]
        hints = get_type_hints(func)

        # Remove return annotation if present
        if "return" in hints:
            hints.pop("return", None)

        # Python 2.7 兼容性：统一将字符串参数转换为 unicode
        def to_unicode_item(v):
            return to_unicode_safe(v) if isinstance(v, (str, bytes)) else v

        # 获取参数默认值信息以支持可选参数
        try:
            _args, _, _, _defaults = inspect.getargspec(func)
            _args = [a for a in _args if a != "self"]
            _num_defaults = len(_defaults) if _defaults else 0
            _num_required = len(_args) - _num_defaults
        except Exception:
            _args = list(hints.keys())
            _num_required = len(_args)

        def validate_list(params, hints):
            if len(params) < _num_required or len(params) > len(_args):
                raise JSONRPCError(
                    -32602,
                    u"Expected {0}-{1} args, got {2}".format(
                        _num_required, len(_args), len(params)
                    ),
                )
            return [to_unicode_item(p) for p in params]

        def validate_dict(params, hints):
            extra = set(params.keys()) - set(hints.keys())
            if extra:
                raise JSONRPCError(-32602, u"Unexpected params: {0}".format(list(extra)))
            # 检查必需参数是否存在
            required_keys = set(_args[:_num_required])
            missing = required_keys - set(params.keys())
            if missing:
                raise JSONRPCError(
                    -32602, u"Missing required params: {0}".format(list(missing))
                )
            return {k: to_unicode_item(params[k]) for k in params}

        if isinstance(params, list):
            return func(*validate_list(params, hints))
        elif isinstance(params, dict):
            return func(**validate_dict(params, hints))
        else:
            raise JSONRPCError(
                -32600, "Invalid Request: params must be array or object"
            )


rpc_registry = RPCRegistry()


def jsonrpc(func):
    """Decorator to register a function as a JSON-RPC method"""
    global rpc_registry
    return rpc_registry.register(func)


class JSONRPCRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def send_jsonrpc_error(self, code, message, id=None):
        response = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": to_unicode_safe(message)},
        }
        if id is not None:
            response["id"] = id
        response_body = json.dumps(response)
        if isinstance(response_body, _unicode_type):
            response_body = response_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(response_body))
        self.end_headers()
        self.wfile.write(response_body)

    def do_POST(self):
        global rpc_registry

        parsed_path = urlparse(self.path)
        if parsed_path.path != "/mcp":
            self.send_jsonrpc_error(-32098, "Invalid endpoint", None)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_jsonrpc_error(-32700, "Parse error: missing request body", None)
            return

        if content_length > 10 * 1024 * 1024:  # 10MB limit
            self.send_jsonrpc_error(-32600, "Request too large", None)
            return

        request_body = self.rfile.read(content_length)
        try:
            request = json.loads(request_body)
        except ValueError:  # Python 2.7 uses ValueError instead of JSONDecodeError
            self.send_jsonrpc_error(-32700, "Parse error: invalid JSON", None)
            return

        # Prepare the response
        response = {"jsonrpc": "2.0"}
        if request.get("id") is not None:
            response["id"] = request.get("id")

        try:
            # Basic JSON-RPC validation
            if not isinstance(request, dict):
                raise JSONRPCError(-32600, "Invalid Request")
            if request.get("jsonrpc") != "2.0":
                raise JSONRPCError(-32600, "Invalid JSON-RPC version")
            if "method" not in request:
                raise JSONRPCError(-32600, "Method not specified")

            # Dispatch the method
            result = rpc_registry.dispatch(request["method"], request.get("params", []))
            response["result"] = result

        except JSONRPCError as e:
            response["error"] = {"code": e.code, "message": e.message}
            if e.data is not None:
                response["error"]["data"] = e.data
        except (Exception, JavaThrowable) as e:
            traceback.print_exc()
            response["error"] = {
                "code": -32603,
                "message": "Internal error (please report a bug)",
                "data": to_unicode_safe(traceback.format_exc()),
            }

        try:
            response_body = json.dumps(response)
        except (Exception, JavaThrowable):
            traceback.print_exc()
            # fallback: format_exc as string but safely decode it
            tb = traceback.format_exc()
            tb_safe = to_unicode_safe(tb)

            response_body = json.dumps(
                {
                    "error": {
                        "code": -32603,
                        "message": "Internal error (please report a bug)",
                        "data": tb_safe,
                    }
                }
            )

        if isinstance(response_body, _unicode_type):
            response_body = response_body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(response_body))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        # Suppress logging
        pass


class MCPHTTPServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    allow_reuse_address = True


class Server(object):  # Use explicit inheritance from object for py2
    HOST = os.getenv("JEB_MCPC_HOST", "127.0.0.1")
    PORT = int(os.getenv("JEB_MCPC_PORT", "16161"))

    def __init__(self):
        self.server = None
        self.server_thread = None
        self.running = False

    def start(self):
        if self.running:
            print("[MCP] Server is already running")
            return

        # Python 2.7 doesn't support daemon parameter in Thread constructor
        self.server_thread = threading.Thread(target=self._run_server)
        self.server_thread.daemon = True  # Set daemon attribute after creation
        self.running = True
        self.server_thread.start()

    def stop(self):
        if not self.running:
            return

        self.running = False
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        if self.server_thread:
            try:
                self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server = None
        print("[MCP] Server stopped")

    def _run_server(self):
        try:
            # Create server in the thread to handle binding
            self.server = MCPHTTPServer(
                (Server.HOST, Server.PORT), JSONRPCRequestHandler
            )
            print(
                u"[MCP] Server started at http://{0}:{1}".format(
                    to_unicode_safe(Server.HOST), Server.PORT
                )
            )
            self.server.serve_forever()
        except OSError as e:
            if getattr(e, 'errno', None) in (98, 10048):  # Port already in use (Linux/Windows)
                print(u"[MCP] Error: Port {0} is already in use".format(Server.PORT))
            else:
                print(u"[MCP] Server error: {0}".format(to_unicode_safe(e)))
            self.running = False
        except (Exception, JavaThrowable) as e:
            print(u"[MCP] Server error: {0}".format(to_unicode_safe(e)))
        finally:
            self.running = False


# 定义为 unicode 字符串 (u'...')
# PERF-4: 使用预编译正则替代逐字符遍历，提升 Manifest 预处理性能
_ILLEGAL_XML_CHARS_RE = re.compile(
    u'[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD]'
)


def preprocess_manifest_py2(manifest_text):
    """
    一个为 Python 2 设计的、健壮的 Manifest 预处理函数。
    它会清理非法字符，并保持 XML 标签结构完整。
    """
    manifest_text = to_unicode_safe(manifest_text)
    return _ILLEGAL_XML_CHARS_RE.sub(u'', manifest_text)


@jsonrpc
def ping():
    """Do a simple ping to check server is alive and running"""
    return "pong"


# implement a FIFO queue to store the artifacts (线程安全加锁防护)
artifactQueue = list()
_artifact_queue_lock = threading.Lock()


def addArtifactToQueue(artifact):
    """Add an artifact to the queue"""
    with _artifact_queue_lock:
        artifactQueue.append(artifact)


def getArtifactFromQueue():
    """Get an artifact from the queue"""
    with _artifact_queue_lock:
        if len(artifactQueue) > 0:
            return artifactQueue.pop(0)
        return None


def clearArtifactQueue():
    """Clear the artifact queue"""
    global artifactQueue
    with _artifact_queue_lock:
        artifactQueue = list()


MAX_OPENED_ARTIFACTS = 1

# 全局缓存管理 (LRU 思想)
# 限制缓存条目数量以保护内存
MAX_CACHE_ENTRIES = 10
apk_cached_data = {}
apk_cache_order = []
_cache_lock = threading.Lock()


def _add_to_cache(key, value):
    """添加数据到缓存并维护顺序，超过限制时弹出最早的数据（线程安全）"""
    with _cache_lock:
        if key in apk_cached_data:
            apk_cache_order.remove(key)
        elif len(apk_cached_data) >= MAX_CACHE_ENTRIES:
            oldest = apk_cache_order.pop(0)
            print(u"[MCP] Cache eviction: popping %s" % to_unicode_safe(oldest))
            del apk_cached_data[oldest]

        apk_cached_data[key] = value
        apk_cache_order.append(key)


def _get_from_cache(key):
    """从缓存获取数据并将 key 移至最新（线程安全）"""
    with _cache_lock:
        if key in apk_cached_data:
            apk_cache_order.remove(key)
            apk_cache_order.append(key)
            return apk_cached_data[key]
        return None


def clear_apk_cache():
    """清理所有缓存（线程安全）"""
    with _cache_lock:
        apk_cached_data.clear()
        del apk_cache_order[:]


def getOrLoadApk(filepath):
    if CTX is None:
        raise JSONRPCError(-1, "[Error] JEB Context (CTX) not bound.")
    engctx = CTX.getEnginesContext()
    if not engctx:
        print("Back-end engines not initialized")
        raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)

    if not filepath:
        # 尝试返回当前已经在 JEB 中打开的活动 APK
        projects = engctx.getProjects()
        if projects and len(projects) > 0:
            prj = projects[0]

            # 使用 JEB 官方工具寻找已经建立连接的 APK 单元
            apks = RuntimeProjectUtil.findUnitsByType(prj, IApkUnit, False)
            if apks and len(apks) > 0:
                print("[MCP] No filepath provided, returning active APK unit.")
                return apks[0]

            # Fallback 策略
            for artifact in prj.getLiveArtifacts():
                unit = artifact.getMainUnit()
                if isinstance(unit, IApkUnit):
                    print("[MCP] No filepath provided, returning active APK artifact.")
                    return unit
        raise JSONRPCError(
            -1,
            "[Error] No active APK currently opened in JEB, please specify filepath.",
        )

    if not os.path.exists(filepath):
        print(u"File not found: %s" % to_unicode_safe(filepath))
        raise JSONRPCError(-1, ErrorMessages.LOAD_APK_NOT_FOUND)

    # Load or create the project in the same directory as the APK file
    project_path = filepath + ".jdb2"
    project = engctx.loadProject(project_path)
    correspondingArtifact = None
    for artifact in project.getLiveArtifacts():
        if artifact.getArtifact().getName() == filepath:
            # If the artifact is already loaded, return it
            correspondingArtifact = artifact
            break

    if correspondingArtifact:
        # Update its position in the queue to mark it as most recently used
        with _artifact_queue_lock:
            if correspondingArtifact in artifactQueue:
                artifactQueue.remove(correspondingArtifact)
                artifactQueue.append(correspondingArtifact)
    else:
        # try to load the artifact, but first check if the queue size has been exceeded
        # ROBUST-4: 仅在队列达到上限时才移除，避免清空所有已加载 artifact
        while len(artifactQueue) >= MAX_OPENED_ARTIFACTS:
            # unload the oldest artifact
            oldestArtifact = getArtifactFromQueue()
            if not oldestArtifact:
                break
            oldestArtifactName = oldestArtifact.getArtifact().getName()
            print(
                u"Unloading artifact: %s because queue size limit exceeded"
                % to_unicode_safe(oldestArtifactName)
            )
            try:
                RuntimeProjectUtil.destroyLiveArtifact(oldestArtifact)
            except (Exception, JavaThrowable) as e:
                print(u"[MCP] Error destroying artifact: %s" % to_unicode_safe(e))

        # Fix: 直接用filepath而不是basename作为Artifact的名称，否则如果加载了多个同名不同路径的apk，会出现问题。
        correspondingArtifact = project.processArtifact(
            Artifact(filepath, FileInput(File(filepath)))
        )
        if not correspondingArtifact:
            raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)
        addArtifactToQueue(correspondingArtifact)
        clear_apk_cache()

    unit = correspondingArtifact.getMainUnit()
    if isinstance(unit, IApkUnit):
        # If the unit is already loaded, return it
        return unit
    raise JSONRPCError(-1, ErrorMessages.LOAD_APK_FAILED)


@jsonrpc
def get_manifest(filepath):
    """Get the manifest of the given APK file in path, note filepath needs to be an absolute path"""

    # Use optimized cache
    cache_key = "manifest_" + to_unicode_safe(filepath)
    cached_text = _get_from_cache(cache_key)
    if cached_text:
        return cached_text

    apk = getOrLoadApk(filepath)
    man = apk.getManifest()
    if man is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    text = _extract_text_content(man)
    if text is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    _add_to_cache(cache_key, text)
    return text


@jsonrpc
def get_exported_components(filepath, component_type):
    """
    Get all exported components of the specified type from the APK manifest.
    A component is considered "exported" if:
    - It explicitly sets android:exported="true", or
    - It omits android:exported but includes an <intent-filter> (implicitly exported).
    component_type can be: 'activity', 'service', 'receiver', 'provider'.
    Returns a list of fully qualified exported component class names.
    """
    if not component_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    # 校验组件类型
    valid_types = ("activity", "service", "receiver", "provider")
    if component_type not in valid_types:
        raise JSONRPCError(
            -1,
            "[Error] Invalid component_type. Must be one of: " + ", ".join(valid_types),
        )

    cache_key = "exported_" + component_type + "s_" + to_unicode_safe(filepath)

    # 首先尝试在缓存中取，跳过XML解析。
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    manifest_text = get_manifest(filepath)
    manifest_text = preprocess_manifest_py2(manifest_text)

    if not manifest_text:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    try:
        root = ET.fromstring(manifest_text.encode("utf-8") if isinstance(manifest_text, _unicode_type) else manifest_text)
    except (Exception, JavaThrowable) as e:
        print(u"[MCP] Error parsing manifest: {0}".format(to_unicode_safe(e)))
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    ANDROID_NS = "http://schemas.android.com/apk/res/android"
    exported_components = []

    # 获取包名
    package_name = root.attrib.get("package", "").strip()

    # 查找 <application> 节点
    app_node = root.find("application")
    if app_node is None:
        raise JSONRPCError(-1, ErrorMessages.GET_MANIFEST_FAILED)

    for node in app_node.findall(component_type):
        name = node.attrib.get("{" + ANDROID_NS + "}name")
        exported = node.attrib.get("{" + ANDROID_NS + "}exported")
        has_intent_filter = len(node.findall("intent-filter")) > 0

        if not name:
            continue

        if exported == "true" or (exported is None and has_intent_filter):
            normalized = []
            seen = set()

            def _add_unique(val):
                if val not in seen:
                    seen.add(val)
                    normalized.append(val)

            if name.startswith("."):
                _add_unique(package_name + name)
            elif "." not in name:
                _add_unique(name)
                _add_unique(package_name + "." + name)
            else:
                _add_unique(name)

            exported_components.extend(normalized)

    # 缓存数据
    _add_to_cache(cache_key, exported_components)
    return exported_components


@jsonrpc
def get_smali_code(filepath, item_signature):
    """Get the smali code of the given class or method in the APK file.
    The passed in item_signature needs to be a fully-qualified signature.
    Dex units use Java-style internal addresses to identify items:
    - package: Lcom/abc/
    - type: Lcom/abc/Foo;
    - method: Lcom/abc/Foo;->bar(I[JLjava/Lang/String;)V
    - field: Lcom/abc/Foo;->flag1:Z
    note filepath needs to be an absolute path
    """
    if not item_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Optimized: Find item using unified identifier/signature logic
    item, item_type = find_item_by_signature(codeUnit, item_signature)
    if not item:
        # Check if it was a class or method for proper error message
        if "->" in item_signature:
            print(u"Method not found: {0}".format(to_unicode_safe(item_signature)))
            raise_method_not_found(item_signature)
        else:
            print(u"Class not found: {0}".format(to_unicode_safe(item_signature)))
            raise_class_not_found(item_signature)

    if item_type == "method":
        # BUG-3: 外部方法无 bytecode，getData() 返回 None
        if item.getData() is None:
            return u"[External method - no bytecode available: {0}]".format(to_unicode_safe(item_signature))
        instructions = item.getInstructions()
        lines = []
        if instructions:
            for instruction in instructions:
                lines.append(instruction.format(None))
        return "\n".join(lines)
    elif item_type == "class":
        lines = []
        for method in item.getMethods():
            lines.append("method: " + method.getSignature(True))
            # BUG-3: 跳过外部方法引用
            if method.getData() is None:
                lines.append("  [External method - no bytecode]")
                lines.append("")
                continue
            instructions = method.getInstructions()
            if instructions:
                for instruction in instructions:
                    lines.append(instruction.format(None))
            lines.append("")
        return "\n".join(lines)

    return ""


@jsonrpc
def get_decompiled_code(filepath, item_signature):
    """Get the decompiled code of the given class or method in the APK file.
    The passed in item_signature needs to be a fully-qualified signature.
    Dex units use Java-style internal addresses to identify items:
    - package: Lcom/abc/
    - type: Lcom/abc/Foo;
    - method: Lcom/abc/Foo;->bar(I[JLjava/Lang/String;)V
    - field: Lcom/abc/Foo;->flag1:Z
    note filepath needs to be an absolute path
    """
    if not item_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    decomp = DecompilerHelper.getDecompiler(codeUnit)
    if not decomp:
        print(
            u"Cannot acquire decompiler for unit: {0}".format(to_unicode_safe(codeUnit))
        )
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    # Optimized: Find item using unified identifier/signature logic
    item, item_type = find_item_by_signature(codeUnit, item_signature)
    if not item:
        if "->" in item_signature:
            print(u"Method not found: {0}".format(to_unicode_safe(item_signature)))
            raise_method_not_found(item_signature)
        else:
            print(u"Class not found: {0}".format(to_unicode_safe(item_signature)))
            raise_class_not_found(item_signature)

    if item_type == "method":
        # BUG-1: 使用 getSignature(False) 获取原始内部签名，反编译器使用原始签名索引
        method_sig_internal = item.getSignature(False)
        if not decomp.decompileMethod(method_sig_internal):
            print(
                u"Failed decompiling method: {0}".format(to_unicode_safe(item_signature))
            )
            raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)
        return decomp.getDecompiledMethodText(method_sig_internal)
    elif item_type == "class":
        # BUG-1: 使用 getSignature(False) 获取原始内部签名
        class_sig_internal = item.getSignature(False)
        if not decomp.decompileClass(class_sig_internal):
            print(
                u"Failed decompiling class: {0}".format(to_unicode_safe(item_signature))
            )
            raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)
        return decomp.getDecompiledClassText(class_sig_internal)

    return ""


def find_item_by_signature(codeUnit, item_signature):
    """
    Find a code item (Class, Method, or Field) by its fully-qualified signature.
    Automatically normalizes dot-notation signatures (e.g. com.abc.Foo -> Lcom/abc/Foo;).
    Returns: (item, type_name) where type_name is 'class', 'method', 'field' or ''.
    """
    if not item_signature or not codeUnit:
        return None, ""

    # Harmonize signature to unicode for non-ASCII characters in Jython 2.7
    item_signature = to_unicode_safe(item_signature).strip()

    # 1. Normalize dot notation if needed
    sig = item_signature
    if not (sig.startswith("L") and (sig.endswith(";") or ";->" in sig)):
        if "." in sig or not sig.startswith("L"):
            if "->" in sig:
                parts = sig.split("->", 1)
                cls_part = parts[0]
                if not cls_part.startswith("L"):
                    cls_part = "L" + cls_part.replace(".", "/")
                if not cls_part.endswith(";"):
                    cls_part += ";"
                sig = cls_part + "->" + parts[1]
            else:
                sig = "L" + sig.replace(".", "/") + ";"

    item = None
    # 2. Try to determine type by signature pattern
    if sig.startswith("L") and sig.endswith(";"):
        item = codeUnit.getClass(sig)
        if item:
            return item, "class"
        # BUG-5: 递归尝试多层内部类匹配
        # com.abc.Foo.Inner.Deeper -> Lcom/abc/Foo$Inner$Deeper;
        if "/" in sig:
            test_sig = sig
            while "/" in test_sig[2:]:
                idx = test_sig.rfind("/")
                test_sig = test_sig[:idx] + "$" + test_sig[idx+1:]
                item = codeUnit.getClass(test_sig)
                if item:
                    return item, "class"
    elif "->" in sig:
        if "(" in sig:
            item = codeUnit.getMethod(sig)
            if item:
                return item, "method"
        else:
            item = codeUnit.getField(sig)
            if item:
                return item, "field"

    # 3. Generic fallback if pattern didn't match or direct lookup failed
    lookups = [
        (codeUnit.getClass, "class"),
        (codeUnit.getMethod, "method"),
        (codeUnit.getField, "field"),
    ]
    for fn, type_name in lookups:
        try:
            item = fn(sig)
            if item:
                return item, type_name
        except (Exception, JavaThrowable):
            pass

    return None, ""



@jsonrpc
def get_method_overrides(filepath, method_signature):
    """
    Get the overrides of the given method in the APK file, the passed in method_signature needs to be a fully-qualified signature
    note filepath needs to be an absolute path
    """
    if not method_signature:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)
    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    method, item_type = find_item_by_signature(codeUnit, method_signature)
    if method is None or item_type != "method":
        print(u"Method not found: {0}".format(to_unicode_safe(method_signature)))
        raise_method_not_found(method_signature)
    ret = []
    data = ActionOverridesData()
    actionContext = ActionContext(
        codeUnit, Actions.QUERY_OVERRIDES, method.getItemId(), None
    )
    if codeUnit.canExecuteAction(actionContext) and codeUnit.prepareExecution(actionContext, data):
        addrs = data.getAddresses()
        if addrs is not None:
            for addr in addrs:
                ret.append(addr)
    return ret


@jsonrpc
def get_class_hierarchy(filepath, class_signature, relation_type):
    """
    Get the superclass or interfaces of the given class in the APK file.
    relation_type: 'superclass' or 'interface'.
    """
    if not class_signature or not relation_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    clazz, item_type = find_item_by_signature(codeUnit, class_signature)

    if clazz is None or item_type != "class":
        print(u"Class not found: {0}".format(to_unicode_safe(class_signature)))
        raise_class_not_found(class_signature)

    if relation_type == "superclass":
        return clazz.getSupertypeSignature(True)
    elif relation_type == "interface":
        # ROBUST-1: getInterfaceSignatures 可能返回 null（无接口时）
        ifaces = clazz.getInterfaceSignatures(True)
        return [sig for sig in ifaces] if ifaces else []
    else:
        raise JSONRPCError(
            -1, "[Error] Invalid relation_type. Use 'superclass' or 'interface'."
        )


@jsonrpc
def get_class_members(filepath, class_signature, member_type):
    """
    Get the members (methods or fields) of the given class in the APK file.
    member_type: 'method' or 'field'.
    """
    if not class_signature or not member_type:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    clazz, item_type = find_item_by_signature(codeUnit, class_signature)

    if clazz is None or item_type != "class":
        print(u"Class not found: {0}".format(to_unicode_safe(class_signature)))
        raise_class_not_found(class_signature)

    ret = []
    if member_type == "method":
        items = clazz.getMethods()
    elif member_type == "field":
        items = clazz.getFields()
    else:
        raise JSONRPCError(-1, "[Error] Invalid member_type. Use 'method' or 'field'.")

    if items:
        for item in items:
            if item:
                ret.append(item.getSignature(True))

    return ret


@jsonrpc
def rename_code_item(filepath, item_signature, new_name):
    """
    Rename a code class, method or field to another name, which may be better-suited or more descriptive than the original name.
    """
    if not item_signature or not new_name:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()
    item, item_type = find_item_by_signature(codeUnit, item_signature)

    if item is None:
        print(u"Item not found: {0}".format(to_unicode_safe(item_signature)))
        raise JSONRPCError(-1, u"[Error] Item not found: {0}".format(to_unicode_safe(item_signature)))

    itemId = item.getItemId()
    print(u"[MCP] rename_code_item: item found, itemId={0}, type={1}".format(itemId, to_unicode_safe(item_type)))

    data = ActionRenameData() if ActionRenameData else None
    if data and ActionContext and Actions:
        act_ctx = ActionContext(codeUnit, Actions.RENAME, itemId, None)
        if codeUnit.canExecuteAction(act_ctx) and codeUnit.prepareExecution(act_ctx, data):
            data.setNewName(new_name)
            # Attempt to bypass name checks if it's supported, reducing the likelihood of action failure
            data.setBypassNameChecks(True)
            if codeUnit.executeAction(act_ctx, data):
                print(u"rename item successfully executed via Action Engine: {0}".format(to_unicode_safe(new_name)))
                return True

    # Fallback: 逃生策略 — 尝试直接在底层 ICodeItem 上设置名称
    if hasattr(item, "setName"):
        try:
            res = item.setName(new_name)
            if res:
                print(u"rename item successfully executed via fallback setName: {0}".format(to_unicode_safe(new_name)))
                return True
        except (Exception, JavaThrowable) as e:
            print(u"[MCP] Fallback setName error: {0}".format(to_unicode_safe(e)))

    raise JSONRPCError(-1, u"[Error] Failed to rename item: {0}".format(to_unicode_safe(item_signature)))


def replace_last_once(s, old, new):
    parts = s.rsplit(old, 1)
    return new.join(parts) if len(parts) > 1 else s


@jsonrpc
def check_java_identifier(filepath, identifier):
    """
    Check an identifier in the APK file and recognize if this is a class, method or field.
    the passed in identifier needs to be a fully-qualified name (like `com.abc.def.Foo`) or a signature;
    the passed in filepath needs to be a fully-qualified absolute path;
    the return value will be a list to tell you the possible type of the passed identifier.
    """
    if not identifier:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Normalize input to signature if needed
    sig = identifier
    if not (sig.startswith("L") and (sig.endswith(";") or ";->" in sig)):
        # Try converting from dot notation to signature notation
        if "." in identifier or not identifier.startswith("L"):
            sig = "L" + identifier.replace(".", "/") + ";"

    # Use index-based lookup via find_item_by_signature for O(1) performance
    item, item_type = find_item_by_signature(codeUnit, sig)

    # If not found with original sig, maybe it's a method without signature details
    if not item and ";" in sig and ";->" not in sig:
        # Try as a partial method match via replace_last_once
        fake_sig = replace_last_once(sig, ";", ";->")
        item, item_type = find_item_by_signature(codeUnit, fake_sig)

    result = []
    if item:
        parent_sig = "N/A"
        if item_type == "class":
            parent_sig = item.getPackage().getSignature(True)
        elif item_type in ("method", "field"):
            parent_sig = item.getClassType().getSignature(True)

        result.append(
            {
                "type": item_type,
                "signature": item.getSignature(True),
                "parent": parent_sig,
            }
        )

    if len(result) == 0:
        if (
            identifier.startswith("dalvik")
            or identifier.startswith("Landroid")
            or identifier.startswith("android")
        ):
            result.append(
                {"type": "Android base type", "signature": "N/A", "parent": "N/A"}
            )
        elif identifier.startswith("Ljava") or identifier.startswith("java"):
            result.append(
                {"type": "Java base type", "signature": "N/A", "parent": "N/A"}
            )
        else:
            result.append({"type": "Not found", "signature": "N/A", "parent": "N/A"})

    return result


def _try_rename_in_java_method(decomp, method_sig, old_var_name, new_var_name):
    """
    尝试在一个已反编译的方法中查找并重命名变量。
    优先使用 IDexDecompilerUnit.setIdentifierName(msig, currentName, newName) 直接 API。
    返回 (found: bool, debug_info: list)
    """
    debug_info = []

    # Strategy A: 直接使用 3 参数重载 API（最高效，无需获取 IJavaMethod）
    try:
        if decomp.setIdentifierName(method_sig, old_var_name, new_var_name):
            return True, debug_info
    except (Exception, JavaThrowable) as e:
        debug_info.append("direct_api_err=" + to_unicode_safe(e))

    # Strategy B: Fallback — 通过 IdentifierManager 遍历标识符（兼容旧版 JEB）
    try:
        java_method = decomp.getMethod(method_sig, False)
        if not java_method:
            debug_info.append("java_method=None")
            return False, debug_info

        idmgr = java_method.getIdentifierManager()
        if idmgr:
            all_idents = idmgr.getIdentifiers()
            if all_idents:
                for ident in all_idents:
                    defn = idmgr.getDefinition(ident)
                    iname = None
                    if defn and hasattr(defn, "getName"):
                        iname = defn.getName()
                    if not iname:
                        iname = str(ident)

                    if iname == old_var_name:
                        try:
                            res = decomp.setIdentifierName(ident, new_var_name)
                            if res:
                                return True, debug_info
                        except (Exception, JavaThrowable):
                            pass
                        try:
                            if defn and hasattr(defn, "setName"):
                                defn.setName(new_var_name)
                                return True, debug_info
                        except (Exception, JavaThrowable):
                            pass
        else:
            debug_info.append("idmgr=None")

        # Strategy C: Check method parameters directly (handles p0, p1...)
        try:
            params = java_method.getParameters()
            if params:
                for param in params:
                    if param and param.getIdentifier():
                        if param.getIdentifier().getName() == old_var_name:
                            param.getIdentifier().setName(new_var_name)
                            return True, debug_info
        except (Exception, JavaThrowable) as e:
            debug_info.append("param_err=" + to_unicode_safe(e))
    except (Exception, JavaThrowable) as e:
        debug_info.append("fallback_err=" + to_unicode_safe(e))

    return False, debug_info


@jsonrpc
def rename_pseudo_code_variables(
    filepath, method_signature, old_var_name, new_var_name
):
    """
    Rename one or more local variables or parameters defined in the decompiled pseudo-code of a method.
    The method must have been decompiled first.
    Also supports renaming variables inside lambda expressions by automatically
    searching synthetic lambda methods in the same class.
    """
    if not method_signature or not old_var_name or not new_var_name:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)

    codeUnit = apk.getDex()
    method, item_type = find_item_by_signature(codeUnit, method_signature)
    if not method or item_type != "method":
        raise_method_not_found(method_signature)

    decomp = DecompilerHelper.getDecompiler(codeUnit)
    if not decomp:
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    # Ensure method is decompiled
    # BUG-2: 使用 getSignature(False) 获取原始内部签名，反编译器使用原始签名索引
    method_sig = method.getSignature(False)
    if not decomp.decompileMethod(method_sig):
        raise JSONRPCError(-1, ErrorMessages.DECOMPILE_FAILED)

    all_debug = []

    # Strategy 1: Try in the target method itself (direct API + fallback)
    found, dbg = _try_rename_in_java_method(
        decomp, method_sig, old_var_name, new_var_name
    )
    all_debug.extend(dbg)
    if found:
        # Force cache reload to prevent returning stale data
        try:
            decomp.removeDecompilation(method_sig)
        except (Exception, JavaThrowable):
            pass
        return True

    # Strategy 3: Search lambda/synthetic methods in the same class
    # Lambda variables belong to compiler-generated methods like lambda$xxx$0
    try:
        # Extract class signature from method signature: "Lcom/Foo;->bar()V" -> "Lcom/Foo;"
        class_sig = method_signature.split("->")[0]
        if not class_sig.endswith(";"):
            class_sig = class_sig + ";"
        dex_class = codeUnit.getClass(class_sig)
        if dex_class:
            class_methods = dex_class.getMethods()
            if class_methods:
                for m in class_methods:
                    m_sig = m.getSignature(True)
                    m_name = m.getName(True)
                    # Only check lambda$ and access$ synthetic methods
                    if not m_name:
                        continue
                    if "lambda$" not in m_name and "access$" not in m_name:
                        continue
                    # Skip the original method itself
                    if m_sig == method_sig:
                        continue
                    try:
                        if not decomp.decompileMethod(m_sig):
                            continue
                        found, dbg = _try_rename_in_java_method(
                            decomp, m_sig, old_var_name, new_var_name
                        )
                        if found:
                            try:
                                decomp.removeDecompilation(m_sig)
                                decomp.removeDecompilation(method_sig)
                            except (Exception, JavaThrowable):
                                pass
                            return True
                    except (Exception, JavaThrowable):
                        continue
        else:
            all_debug.append("class_not_found=" + class_sig)

        # Strategy 4: Search inner anonymous classes (e.g. OuterClass$1)
        # PERF-1: 限制内部类扫描数量，避免大型 APK 中 O(N) 全表扫描
        inner_class_prefix = class_sig[:-1] + "$"
        _MAX_INNER_CLASSES_SCAN = 50
        _inner_scan_count = 0
        for clz in codeUnit.getClasses():
            if _inner_scan_count >= _MAX_INNER_CLASSES_SCAN:
                break
            clz_sig = clz.getSignature(True)
            if clz_sig and clz_sig.startswith(inner_class_prefix):
                _inner_scan_count += 1
                for m in clz.getMethods():
                    m_sig = m.getSignature(True)
                    if m_sig == method_sig:
                        continue
                    try:
                        if not decomp.decompileMethod(m_sig):
                            continue
                        found, dbg = _try_rename_in_java_method(
                            decomp, m_sig, old_var_name, new_var_name
                        )
                        # We don't append every single inner class dbg failure to avoid huge error msgs
                        if found:
                            try:
                                decomp.removeDecompilation(m_sig)
                                decomp.removeDecompilation(method_sig)
                            except (Exception, JavaThrowable):
                                pass
                            return True
                    except (Exception, JavaThrowable):
                        continue

    except (Exception, JavaThrowable) as e:
        all_debug.append("lambda_scan_err=" + to_unicode_safe(e))

    msg = u"[Error] Variable '{0}' not found in method {1} (including lambda methods). Debug: {2}".format(
        to_unicode_safe(old_var_name), to_unicode_safe(method_signature), u"; ".join([to_unicode_safe(x) for x in all_debug])
    )
    raise JSONRPCError(-1, msg)


@jsonrpc
def list_cross_references(filepath, address):
    """Retrieve cross-references to an address in a code unit, that is, the users or callers of the item at the provided address."""
    if not address:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    # Default use the dex unit
    codeUnit = apk.getDex()
    if not codeUnit:
        raise JSONRPCError(-1, "[Error] DEX unit not found.")

    item, item_type = find_item_by_signature(codeUnit, address)

    if not item:
        raise JSONRPCError(-1, ErrorMessages.ADDRESS_NOT_FOUND + " " + to_unicode_safe(address))

    item_id = item.getItemId()
    if item_id <= 0:
        raise JSONRPCError(-1, ErrorMessages.ADDRESS_NOT_FOUND + " " + to_unicode_safe(address))

    ret = []
    actionXrefsData = ActionXrefsData()
    actionContext = ActionContext(codeUnit, Actions.QUERY_XREFS, item_id, None)
    if codeUnit.canExecuteAction(actionContext) and codeUnit.prepareExecution(actionContext, actionXrefsData):
        addrs = actionXrefsData.getAddresses()
        dtls = actionXrefsData.getDetails()

        if addrs is not None and dtls is not None:
            num = len(addrs) if hasattr(addrs, "__len__") else addrs.size()
            for i in range(num):
                ret.append(
                    {
                        "address": addrs[i],
                        "details": dtls[i],
                    }
                )
    return ret


@jsonrpc
def list_dex_strings(filepath, pattern=None, limit=1000, offset=0):
    """
    Retrieve the list of strings defined in the dex constants pools.
    Supports filtering and pagination to prevent memory issues.
    """
    apk = getOrLoadApk(filepath)
    if apk is None:
        return []

    codeUnit = apk.getDex()
    if not codeUnit:
        return []

    strings = codeUnit.getStrings()
    results = []
    
    pattern_u = to_unicode_safe(pattern) if pattern else None
    count = 0
    if strings:
        for s in strings:
            val = s.getValue()
            if not val:
                continue
            val_u = to_unicode_safe(val)
            if pattern_u and pattern_u not in val_u:
                continue
                
            if count >= offset:
                results.append(val_u)
                
            count += 1
            if len(results) >= limit:
                break
            
    return results


@jsonrpc
def get_all_classes(filepath, package_prefix=None, limit=1000, offset=0):
    """
    List all classes in the project (from the Dex unit).
    Supports filtering and pagination to prevent memory issues.
    """
    apk = getOrLoadApk(filepath)

    codeUnit = apk.getDex()
    if not codeUnit:
        return []

    classes = codeUnit.getClasses()
    results = []
    
    count = 0
    if classes:
        for c in classes:
            sig = c.getSignature(True)
            if not sig:
                continue
                
            if package_prefix and package_prefix not in sig:
                continue
                
            if count >= offset:
                results.append(sig)
                
            count += 1
            if len(results) >= limit:
                break
            
    return results


def _extract_text_content(unit):
    """
    Unified method to extract text representation from any JEB Unit.
    Handles formatter -> presentation -> document -> text flow.
    Uses continuous block reading to prevent stream truncation.
    """
    if not unit:
        return None

    try:
        formatter = unit.getFormatter()
        if formatter:
            presentation = formatter.getPresentation(0)
            if presentation:
                doc = presentation.getDocument()
                if doc:
                    return TextDocumentUtil.getText(doc)
    except (Exception, JavaThrowable):
        try:
            print("[MCP] Error extracting text: " + str(sys.exc_info()[1]))
        except Exception:
            pass

    # Fallback: try to read raw bytes (up to 1MB)
    stream = None
    try:
        if hasattr(unit, "getInput"):
            inp = unit.getInput()
            if inp:
                stream = inp.getStream()
                if stream:
                    # JEB Input Streams are usually limited length, read it
                    # Jython stream reading might need a byte array
                    chunks = []
                    total_read = 0
                    block_size = 16384
                    buf = zeros(block_size, "b")
                    while total_read < 1024 * 1024:
                        read_len = stream.read(buf, 0, block_size)
                        if read_len <= 0:
                            break
                        if hasattr(buf, "tostring"):
                            chunks.append(buf[:read_len].tostring())
                        elif JavaSystem:
                            try:
                                from java.lang import String as JavaString
                                chunks.append(JavaString(buf, 0, read_len, "ISO-8859-1").getBytes("ISO-8859-1"))
                            except Exception:
                                chunks.append(bytes(buf[:read_len]))
                        else:
                            chunks.append(bytes(buf[:read_len]))
                        total_read += read_len
                    if chunks:
                        return b"".join(chunks)
    except (Exception, JavaThrowable):
        try:
            print("[MCP] Fallback raw read failed: " + str(sys.exc_info()[1]))
        except Exception:
            pass
    finally:
        if stream:
            try:
                stream.close()
            except (Exception, JavaThrowable):
                pass

    return None


def _build_unit_tree_index(root_unit, cache_key):
    """
    Unified iterative DFS to build a path -> Unit index for any tree-like Unit structure.
    Uses LRU caching based on the provided cache_key.
    """
    cached = _get_from_cache(cache_key)
    if cached is not None:
        return cached

    index = {}
    if not root_unit:
        return index

    # Iterative DFS to build index
    stack = [(root_unit, u"")]
    while stack:
        current, current_path = stack.pop()
        try:
            current_path = to_unicode_safe(current_path)
            has_input = hasattr(current, "getInput") and current.getInput() is not None
            children = current.getChildren() if hasattr(current, "getChildren") else None

            if (has_input or not children) and current_path:
                index[current_path] = current

            if children:
                for child in children:
                    try:
                        raw_name = child.getName() if hasattr(child, "getName") and child.getName() else u"unnamed_unit"
                        name = to_unicode_safe(raw_name)
                        new_path = (current_path + u"/" + name) if current_path else name
                        stack.append((child, new_path))
                    except (Exception, JavaThrowable):
                        pass
        except (Exception, JavaThrowable):
            pass

    _add_to_cache(cache_key, index)
    return index


def _find_unit_in_index(index, input_path, prefixes):
    """
    Unified fuzzy path matcher. Supports direct, prefix, and suffix matching.
    """
    input_path = to_unicode_safe(input_path)

    # Step 1: Direct Matching
    leaf = index.get(input_path)
    if leaf:
        return leaf

    # Step 2: Try stripping user-provided prefixes if they exist in input_path
    for prefix in prefixes:
        if input_path.startswith(prefix):
            stripped_path = input_path[len(prefix) :]
            if stripped_path in index:
                return index[stripped_path]

    # Step 3: Try adding prefixes to match index paths
    for prefix in prefixes:
        candidate = prefix + input_path
        if candidate in index:
            return index[candidate]

    # Step 4: Suffix matching (powerful fallback)
    suffix = input_path if input_path.startswith("/") else ("/" + input_path)
    items_iter = index.iteritems() if hasattr(index, "iteritems") else index.items()
    for path, unit in items_iter:
        if path.endswith(suffix):
            return unit

    return None


def _get_resource_index_for_filepath(apk, filepath=""):
    """构建 路径 -> Unit 对象的全量资源索引并缓存 (指定 filepath 避免跨包污染)"""
    cache_key = "resource_index_" + to_unicode_safe(filepath)
    return _build_unit_tree_index(apk.getResources(), cache_key)


def _get_asset_index_for_filepath(apk, filepath=""):
    """构建 路径 -> Unit 对象的全量素材索引并缓存 (指定 filepath 避免跨包污染)"""
    cache_key = "asset_index_" + to_unicode_safe(filepath)
    return _build_unit_tree_index(apk.getAssets(), cache_key)


def _get_resource_index(apk):
    return _get_resource_index_for_filepath(apk, "")


def _get_asset_index(apk):
    return _get_asset_index_for_filepath(apk, "")


@jsonrpc
def get_apk_all_files(filepath, category):
    """
    Retrieve all file names for a given category (resource or asset) from the application.
    category: 'resource' or 'asset'.
    """
    if not category:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    if category == "resource":
        index = _get_resource_index_for_filepath(apk, filepath)
    elif category == "asset":
        index = _get_asset_index_for_filepath(apk, filepath)
    else:
        raise JSONRPCError(-1, "[Error] Invalid category. Use 'resource' or 'asset'.")

    return list(index.keys())


@jsonrpc
def get_apk_file_content(filepath, file_path, category):
    """
    Retrieve the text contents of a file (resource or asset) using its path.
    category: 'resource' or 'asset'.
    """
    if not file_path or not category:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    if category == "resource":
        index = _get_resource_index_for_filepath(apk, filepath)
        prefixes = ["res/", "Resources/res/", "Resources/"]
    elif category == "asset":
        index = _get_asset_index_for_filepath(apk, filepath)
        prefixes = ["assets/", "Resources/assets/", "Resources/"]
    else:
        raise JSONRPCError(-1, "[Error] Invalid category. Use 'resource' or 'asset'.")

    leaf = _find_unit_in_index(index, file_path, prefixes)

    if not leaf:
        msg = u"[Error] {0} not found: {1}".format(category.capitalize(), to_unicode_safe(file_path))
        raise JSONRPCError(-1, msg)

    content = _extract_text_content(leaf)
    if content is None:
        raise JSONRPCError(
            -1,
            u"[Error] Failed to read {0} content or format not supported.".format(
                category
            ),
        )

    return to_unicode_safe(content)


@jsonrpc
def add_comment(filepath, address, comment):
    """
    Add a comment to function, class, field or any address in a code unit.
    The address can be a signature (e.g., Lcom/abc/Foo;->bar()V) or a virtual address.
    """
    if not address or comment is None:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # Support offset syntax: Lcom/abc/Foo;->bar()V+0x10
    lookup_address = address
    offset_str = ""
    if "->" in address and "+" in address:
        parts = address.rsplit("+", 1)
        lookup_address = parts[0]
        offset_str = parts[1]

    item, item_type = find_item_by_signature(codeUnit, lookup_address)

    if item:
        # Align instruction boundary if providing an offset for a method
        if item_type == "method" and offset_str:
            try:
                # strip potential 'h' suffix and parse as hex
                raw_hex = offset_str.lower().replace("h", "").replace("0x", "")
                req_offset = int(raw_hex, 16)

                instructions = item.getInstructions()
                if instructions:
                    valid_offsets = [inst.getOffset() for inst in instructions]
                    if req_offset not in valid_offsets:
                        closest_offset = 0
                        for off in valid_offsets:
                            if off <= req_offset:
                                closest_offset = off
                            else:
                                break
                        # Fix the address to the valid boundary
                        address = lookup_address + u"+0x{0:x}".format(closest_offset)
            except (Exception, JavaThrowable) as e:
                print(
                    u"[MCP] add_comment boundary check failed: {0}".format(to_unicode_safe(e))
                )

        itemId = item.getItemId()
        # Using string formatting carefully for Python 2.7 unicode
        print(
            u"[MCP] add_comment: item found, itemId={0}, address={1}".format(
                itemId, to_unicode_safe(address)
            )
        )

        # JEB comment workflow: prepare -> set -> execute
        data = ActionCommentData()
        # Using the original 'address' which contains the +offset if provided
        act_ctx = ActionContext(codeUnit, Actions.COMMENT, itemId, address)
        if codeUnit.canExecuteAction(act_ctx) and codeUnit.prepareExecution(act_ctx, data):
            data.setNewComment(comment)
            if codeUnit.executeAction(act_ctx, data):
                return True

        # Fallback: trying with address only if it looks like a hex/dec address
        data2 = ActionCommentData()
        act_ctx2 = ActionContext(codeUnit, Actions.COMMENT, 0, address)
        if codeUnit.canExecuteAction(act_ctx2) and codeUnit.prepareExecution(act_ctx2, data2):
            data2.setNewComment(comment)
            if codeUnit.executeAction(act_ctx2, data2):
                return True

        msg = u"[Error] Failed to add comment to item: {0}".format(to_unicode_safe(address))
        raise JSONRPCError(-1, msg)
    else:
        # Handle decimal or hex virtual addresses directly
        try:
            # Check if address is numeric (dec or hex)
            if address.lower().startswith("0x"):
                int(address, 16)
            else:
                int(address)

            data = ActionCommentData()
            act_ctx = ActionContext(codeUnit, Actions.COMMENT, 0, address)
            if codeUnit.canExecuteAction(act_ctx) and codeUnit.prepareExecution(act_ctx, data):
                data.setNewComment(comment)
                if codeUnit.executeAction(act_ctx, data):
                    return True
        except ValueError:
            pass


        msg = u"[Error] Could not resolve address or signature: {0}".format(to_unicode_safe(address))
        raise JSONRPCError(-1, msg)


def _search_in_file_index(index, query, category, limit=1000):
    """
    在文件索引(resource/asset)中搜索路径和文本内容。
    - 路径匹配: 返回 {"type": "path", "category": ..., "path": ...}
    - 内容匹配: 返回 {"type": "content", "category": ..., "path": ..., "matches": [...]}
    支持 limit 避免无休止扫描与内存过度消耗。
    """
    TEXT_EXTENSIONS = (
        ".xml", ".json", ".txt", ".html", ".htm", ".css", ".js",
        ".properties", ".cfg", ".ini", ".yml", ".yaml", ".csv",
        ".smali", ".pro", ".gradle", ".md"
    )
    results = []
    query_u = to_unicode_safe(query)

    items_iter = index.iteritems() if hasattr(index, "iteritems") else index.items()
    for path, unit in items_iter:
        if len(results) >= limit:
            break
        path_u = to_unicode_safe(path)
        # 1. 路径匹配
        if query_u in path_u:
            results.append({"type": "path", "category": category, "path": path_u})

        # 2. 文本内容匹配 (仅对文本类文件)
        path_lower = path_u.lower()
        # 优化：通过后缀快速过滤
        is_text = any(path_lower.endswith(ext) for ext in TEXT_EXTENSIONS)

        if is_text:
            try:
                # 检查输入源大小以避免内存暴涨
                if hasattr(unit, "getInput"):
                    inp = unit.getInput()
                    if inp and inp.getSize() > 2 * 1024 * 1024:  # 2MB 限制
                        continue

                content = _extract_text_content(unit)
                if not content:
                    continue

                content_u = to_unicode_safe(content)
                if query_u in content_u:
                    matching_lines = []
                    lines = content_u.splitlines()
                    for line_idx, line in enumerate(lines, 1):
                        if query_u in line:
                            matching_lines.append(u"L{0}: {1}".format(line_idx, line.strip()))
                            if len(matching_lines) > 50:
                                matching_lines.append(u"... (too many matches)")
                                break
                    if matching_lines:
                        results.append(
                            {
                                "type": "content",
                                "category": category,
                                "path": path_u,
                                "matches": matching_lines,
                            }
                        )
            except (Exception, JavaThrowable):
                pass

    return results


def _append_file_search_results(results, file_results, unit_label, limit=1000):
    """将 _search_in_file_index 的结果转换并追加到 results 列表中"""
    for r in file_results:
        if len(results) >= limit:
            break
        if r["type"] == "content":
            for m in r["matches"]:
                if len(results) >= limit:
                    break
                results.append(
                    {
                        "Text": m,
                        "Unit": unit_label,
                        "Document": r["path"],
                        "Location": r["path"],
                    }
                )
        elif r["type"] == "path":
            results.append(
                {
                    "Text": r["path"],
                    "Unit": unit_label,
                    "Document": "Directory/File",
                    "Location": r["path"],
                }
            )


@jsonrpc
def search_in_project(filepath, query, search_type="string", limit=1000):
    """
    Search for strings or identifiers (classes/methods) in the project.
    search_type can be 'string' (default), 'identifier', 'resource', or 'asset'.
    - 'string': search DEX string pool for matching values.
    - 'identifier': search class/method signatures.
    - 'resource': search resource file paths and text content (xml, json, etc).
    - 'asset': search asset file paths and text content.
    Supports limit parameter to prevent memory overflow on large query outputs.
    """
    if not query:
        raise JSONRPCError(-1, ErrorMessages.MISSING_PARAM)

    try:
        limit = int(limit)
    except Exception:
        limit = 1000

    query_u = to_unicode_safe(query)
    apk = getOrLoadApk(filepath)
    results = []

    if search_type == "string":
        codeUnit = apk.getDex()
        if codeUnit:
            unit_name = (
                codeUnit.getName() if hasattr(codeUnit, "getName") else "Bytecode"
            )
            for s in codeUnit.getStrings():
                val = s.getValue()
                if val:
                    val_u = to_unicode_safe(val)
                    if query_u in val_u:
                        loc_addr = (
                            u"0x{0:X}".format(s.getItemId())
                            if hasattr(s, "getItemId")
                            else u""
                        )
                        results.append(
                            {
                                "Text": val_u,
                                "Unit": unit_name,
                                "Document": "String pool",
                                "Location": loc_addr,
                            }
                        )
                        if len(results) >= limit:
                            return results

        # Search resources and assets to mimic 'Entire Project' search
        if len(results) < limit:
            _append_file_search_results(
                results,
                _search_in_file_index(_get_resource_index_for_filepath(apk, filepath), query_u, "resource", limit - len(results)),
                "Resources",
                limit,
            )
        if len(results) < limit:
            _append_file_search_results(
                results,
                _search_in_file_index(_get_asset_index_for_filepath(apk, filepath), query_u, "asset", limit - len(results)),
                "Assets",
                limit,
            )

    elif search_type == "identifier":
        codeUnit = apk.getDex()
        if not codeUnit:
            raise JSONRPCError(-1, "[Error] DEX unit not found.")
        unit_name = codeUnit.getName() if hasattr(codeUnit, "getName") else "Bytecode"
        for c in codeUnit.getClasses():
            sig = c.getSignature(True)
            if sig:
                sig_u = to_unicode_safe(sig)
                if query_u in sig_u:
                    results.append(
                        {
                            "Text": sig_u,
                            "Unit": unit_name,
                            "Document": "Class",
                            "Location": sig_u,
                        }
                    )
                    if len(results) >= limit:
                        return results
        for m in codeUnit.getMethods():
            sig = m.getSignature(True)
            if sig:
                sig_u = to_unicode_safe(sig)
                if query_u in sig_u:
                    results.append(
                        {
                            "Text": sig_u,
                            "Unit": unit_name,
                            "Document": "Method",
                            "Location": sig_u,
                        }
                    )
                    if len(results) >= limit:
                        return results

    elif search_type == "resource":
        _append_file_search_results(
            results,
            _search_in_file_index(_get_resource_index_for_filepath(apk, filepath), query_u, "resource", limit),
            "Resources",
            limit,
        )
    elif search_type == "asset":
        _append_file_search_results(
            results,
            _search_in_file_index(_get_asset_index_for_filepath(apk, filepath), query_u, "asset", limit),
            "Assets",
            limit,
        )
    else:
        raise JSONRPCError(
            -1,
            "[Error] Invalid search_type. Use 'string', 'identifier', 'resource', or 'asset'.",
        )

    return results



# 规则文件缓存，避免每次扫描都重新读取磁盘
_rules_file_cache = {}
_rules_cache_lock = threading.Lock()


def _load_json_rules(path):
    """
    Load JSON rules from a file, compatible with Jython 2.7.
    Results are cached to avoid redundant disk I/O. Thread-safe.
    """
    with _rules_cache_lock:
        if path in _rules_file_cache:
            return _rules_file_cache[path]

    if not os.path.exists(path):
        with _rules_cache_lock:
            _rules_file_cache[path] = None
        return None
    try:
        with open(path, "rb") as f:
            content = f.read()
            content = to_unicode_safe(content)
            data = json.loads(content)
            with _rules_cache_lock:
                _rules_file_cache[path] = data
            return data
    except (Exception, JavaThrowable) as e:
        print(u"[MCP] Error loading JSON rules from {0}: {1}".format(to_unicode_safe(path), to_unicode_safe(e)))
        with _rules_cache_lock:
            _rules_file_cache[path] = None
        return None


def _scan_apk_for_packers(apk):
    """
    Internal helper to scan APK units for packer signatures (File-based).
    Inspired by ApkCheckPack.
    """
    results = []
    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules_dir = os.path.join(script_dir, "rules")

    # Load rules
    # from https://github.com/moyuwa/ApkCheckPack/tree/main/data
    apkpack_data = _load_json_rules(os.path.join(rules_dir, "apkpackdata.json"))
    sdk_data = _load_json_rules(os.path.join(rules_dir, "sdk.json"))

    # Collect all file paths and potentially contents in APK
    all_paths = set()
    all_contents_list = []  # Use a list for safer joining

    # Iterative DFS to collect all unit paths (avoids stack overflow on deep trees)
    stack = [(apk, "")]
    while stack:
        current_unit, current_path = stack.pop()
        try:
            name = current_unit.getName()
            if name:
                name = to_unicode_safe(name)
                all_paths.add(name)
                # Full composite path
                full_p = (current_path + "/" + name) if current_path else name
                all_paths.add(full_p)

                # Content extraction
                if (
                    name.endswith(".xml")
                    or name.endswith(".txt")
                    or name.endswith(".json")
                ):
                    # 防止由于恶意打包大尺寸文本导致 Jython 内存溢出
                    if hasattr(current_unit, "getInput"):
                        inp = current_unit.getInput()
                        if inp and inp.getSize() > 2 * 1024 * 1024:
                            continue

                    content = _extract_text_content(current_unit)
                    if content:
                        all_contents_list.append(to_unicode_safe(content))
            else:
                full_p = current_path

            children = current_unit.getChildren()
            if children:
                for child in children:
                    stack.append((child, full_p))
        except (Exception, JavaThrowable):
            pass

    # 优化：逐文件搜索而非拼接为单一巨大字符串，避免 Jython OOM
    def _has_match_in_contents(sub_str):
        if not sub_str or not all_contents_list:
            return False
        sub_u = to_unicode_safe(sub_str)
        for content_piece in all_contents_list:
            if sub_u in content_piece:
                return True
        return False

    # Match against apkpackdata.json
    if apkpack_data:
        items_iter = apkpack_data.iteritems() if hasattr(apkpack_data, "iteritems") else apkpack_data.items()
        for packer_name, rules in items_iter:
            hit = False
            matched_feature = None

            # soname
            sonames = rules.get("soname", [])
            for sn in sonames:
                for ap in all_paths:
                    if sn in ap:
                        hit = True
                        matched_feature = "SO: " + sn
                        break
                if hit:
                    break

            # other (files)
            if not hit:
                others = rules.get("other", [])
                for ot in others:
                    if ot in all_paths:
                        hit = True
                        matched_feature = "File: " + ot
                        break

            # Match content
            if not hit and all_contents_list:
                others = rules.get("other", [])
                for ot in others:
                    if _has_match_in_contents(ot):
                        hit = True
                        matched_feature = "Content: " + ot
                        break

                if not hit:
                    keywords = rules.get("keywords", [])
                    for kw in keywords:
                        if _has_match_in_contents(kw):
                            hit = True
                            matched_feature = "Keyword: " + kw
                            break


            if hit:
                results.append(
                    {
                        u"category": u"Packer (ApkCheckPack)",
                        u"name": to_unicode_safe(packer_name),
                        u"detail": to_unicode_safe(matched_feature),
                    }
                )

    # Match against sdk.json (List of dicts: {"soname": "...", "zh": {"label": "..."}})
    if sdk_data:
        for sdk_entry in sdk_data:
            sn = sdk_entry.get("soname")
            if not sn:
                continue

            hit = False
            for ap in all_paths:
                if sn in ap:
                    hit = True
                    break

            if hit:
                label = (
                    sdk_entry.get(u"zh", {}).get(u"label")
                    or sdk_entry.get(u"en", {}).get(u"label")
                    or sn
                )
                dev = (
                    sdk_entry.get(u"zh", {}).get(u"dev_team")
                    or sdk_entry.get(u"en", {}).get(u"dev_team")
                    or u"Unknown"
                )
                results.append(
                    {
                        u"category": u"SDK",
                        u"name": to_unicode_safe(label),
                        u"detail": u"Team: {0} (Match: {1})".format(to_unicode_safe(dev), to_unicode_safe(sn)),
                    }
                )

    return results


_u_apis_cache = None
_u_apis_regex_cache = None


def _load_sensitive_apis():
    global _u_apis_cache, _u_apis_regex_cache
    if _u_apis_cache is not None:
        return _u_apis_cache, _u_apis_regex_cache

    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules_dir = os.path.join(script_dir, "rules")
    config_path = os.path.join(rules_dir, "sensitive_strings.txt")

    u_apis = []
    if os.path.exists(config_path):
        try:
            with open(config_path, "rb") as f:
                for line_b in f:
                    line_b = line_b.strip()
                    if line_b and not line_b.startswith(b"#"):
                        u_apis.append(to_unicode_safe(line_b))
        except (Exception, JavaThrowable) as e:
            print(u"[MCP] Error reading sensitive APIs: {0}".format(to_unicode_safe(e)))

    _u_apis_cache = u_apis
    if u_apis:
        pattern = "|".join(re.escape(r) for r in u_apis)
        _u_apis_regex_cache = re.compile("(" + pattern + ")")
    else:
        _u_apis_regex_cache = None

    return _u_apis_cache, _u_apis_regex_cache


@jsonrpc
def perform_security_scan(filepath):
    """
    Performs a comprehensive security scan on the APK, covering three areas:
    1. Packer Detection: Identifies known packers/protectors via file signature matching (rules from 'apkpackdata.json').
    2. SDK Identification: Detects embedded third-party SDKs by matching native library names (rules from 'sdk.json').
    3. Sensitive String/API Scan: Searches the DEX string pool, method and class signatures for sensitive patterns (rules from 'sensitive_strings.txt').
    Returns a flat list of dicts, each with 'type' ('Packer', 'SDK', or 'Sensitive String'), 'name', and 'detail'.
    """
    apk = getOrLoadApk(filepath)
    codeUnit = apk.getDex()

    # 1. Load sensitive APIs (cached)
    u_apis, regex = _load_sensitive_apis()

    # 2. Packer & SDK Scan
    print(u"[MCP] Phase 1/3: Scanning file signatures for Packers and SDKs...")
    all_file_results = _scan_apk_for_packers(apk)

    packer_results = [
        r for r in all_file_results if r[u"category"] == u"Packer (ApkCheckPack)"
    ]
    sdk_results = [r for r in all_file_results if r[u"category"] == u"SDK"]

    # 3. DEX Sensitive Scan
    dex_results = []
    if codeUnit:
        print(u"[MCP] Phase 2/3: Scanning DEX string pool for Sensitive APIs...")
        dex_hits = {}

        if regex:
            def match_content(content):
                if not content:
                    return
                matches = regex.findall(content)
                if matches:
                    for rule in set(matches):
                        if rule not in dex_hits:
                            dex_hits[rule] = set()
                        if len(dex_hits[rule]) < 10:
                            dex_hits[rule].add(content)

            # 优化的 DEX 字符串匹配：DEX 规范中所有类名、方法名和字段名都在 getStrings 缓冲池中
            for s in codeUnit.getStrings():
                match_content(to_unicode_safe(s.getValue()))

        print(u"[MCP] Phase 3/3: Finalizing DEX results...")
        for rule in u_apis:
            if rule in dex_hits:
                unique_matches = list(dex_hits[rule])
                dex_results.append(
                    {
                        "category": "Sensitive API/String",
                        "name": rule,
                        "count": len(unique_matches),
                        "matches": unique_matches[:10],
                    }
                )

    # Combine results into a flat list as required by the return type list[dict]
    final_results = []

    for p in packer_results:
        final_results.append(
            {u"type": u"Packer", u"name": p[u"name"], u"detail": p[u"detail"]}
        )

    for s in sdk_results:
        final_results.append({u"type": u"SDK", u"name": s[u"name"], u"detail": s[u"detail"]})

    for d in dex_results:
        final_results.append(
            {
                u"type": u"Sensitive String",
                u"name": d[u"name"],
                u"detail": u"Hits: {0}".format(d[u"count"]),
            }
        )

    return final_results


@jsonrpc
def export_all_resources(filepath, output_dir=""):
    """
    Export all accessible resources and assets to a local directory structure.
    If output_dir is empty, defaults to a 'dump' directory next to the APK.
    """
    apk = getOrLoadApk(filepath)

    if not output_dir:
        # Determine the default output directory from the active Artifact
        apk_path = None
        engctx = CTX.getEnginesContext() if CTX else None
        if engctx and engctx.getProjects():
            prj = engctx.getProjects()[0]
            for art in prj.getLiveArtifacts():
                if art.getMainUnit() == apk:
                    apk_path = art.getArtifact().getName()
                    break

        if apk_path and os.path.exists(apk_path):
            base = os.path.splitext(apk_path)[0]
            output_dir = base + "_dump"
        else:
            output_dir = os.path.join(os.getcwd(), "dump")

    output_dir = os.path.abspath(output_dir)

    # 1. Export Resources
    res_index = _get_resource_index_for_filepath(apk, filepath)
    # 2. Export Assets
    asset_index = _get_asset_index_for_filepath(apk, filepath)

    # 兜底逻辑 1: 如果 getAssets() 为空，尝试从根 unit 的所有子 unit 查找
    if not asset_index:
        for child in (apk.getChildren() if hasattr(apk, "getChildren") else []):
            c_name = to_unicode_safe(child.getName()).lower() if hasattr(child, "getName") and child.getName() else ""
            # 如果名字包含 asset，或者是未知混淆子单元但包含 Input
            if "asset" in c_name or (hasattr(child, "getInput") and child.getInput() and c_name not in ["bytecode", "resources", "manifest", "strings"]):
                sub_index = _build_unit_tree_index(child, "asset_fallback_" + to_unicode_safe(filepath))
                if sub_index:
                    asset_index.update(sub_index)

    # 从 res_index 中分离出真正的 assets 路径
    real_res_index = {}
    real_asset_index = dict(asset_index)

    for p, u in (res_index.iteritems() if hasattr(res_index, "iteritems") else res_index.items()):
        p_u = to_unicode_safe(p)
        if p_u.startswith("assets/") or p_u.startswith("assets\\") or "/assets/" in p_u:
            real_asset_index[p_u] = u
        else:
            real_res_index[p_u] = u

    res_index = real_res_index
    asset_index = real_asset_index
    total = len(res_index) + len(asset_index)

    # 兜底逻辑 2 (终极保底): 如果 JEB DOM 树中依然没有任何 assets 节点，直接解压底层 APK Zip 中的 assets 条目
    if not asset_index and filepath and os.path.exists(filepath):
        try:
            import zipfile
            with zipfile.ZipFile(filepath, "r") as zf:
                target_assets_dir = os.path.abspath(os.path.join(output_dir, "assets"))
                zip_manifest = {}
                zip_count = 0
                for info in zf.infolist():
                    name_u = to_unicode_safe(info.filename)
                    # 检查是否位于 assets 目录下或文件名属于非 ascii 混淆 Asset
                    if name_u.startswith("assets/") or name_u.startswith("assets\\") or not (name_u.startswith("res/") or name_u == "AndroidManifest.xml" or name_u.endswith(".dex") or name_u.startswith("META-INF/")):
                        if info.is_dir() if hasattr(info, "is_dir") else name_u.endswith("/"):
                            continue
                        zip_count += 1
                        has_non_ascii = any(ord(c) < 32 or ord(c) > 126 for c in name_u)
                        if has_non_ascii or len(name_u) > 100:
                            flat_name = "asset_{0:03d}_{1}.bin".format(zip_count, hashlib.md5(name_u.encode("utf-8")).hexdigest()[:8])
                            out_file = os.path.join(target_assets_dir, flat_name)
                            zip_manifest[flat_name] = {"original_path": name_u, "size": info.file_size}
                        else:
                            rel_p = name_u[7:] if name_u.startswith("assets/") else name_u
                            out_file = os.path.abspath(os.path.join(target_assets_dir, rel_p))
                        
                        out_dir_p = os.path.dirname(out_file)
                        if not os.path.exists(out_dir_p):
                            os.makedirs(out_dir_p)
                        with open(out_file, "wb") as f_out:
                            f_out.write(zf.read(info.filename))
                if zip_manifest:
                    if not os.path.exists(target_assets_dir):
                        os.makedirs(target_assets_dir)
                    with open(os.path.join(target_assets_dir, "assets_manifest.json"), "w") as f_m:
                        json.dump(zip_manifest, f_m, indent=2, ensure_ascii=False)
                if zip_count > 0:
                    print(u"[MCP] Extracted {0} assets files directly from APK Zip archive.".format(zip_count))
        except (Exception, JavaThrowable) as e_zip:
            print(u"[MCP] Zip fallback extraction failed: {0}".format(to_unicode_safe(e_zip)))

    success = 0
    manifest_map = {}

    # helper for exporting
    def save_units(index, base_name):
        curr_success = 0
        target_base = os.path.abspath(os.path.join(output_dir, base_name))

        items_iter = index.iteritems() if hasattr(index, "iteritems") else index.items()
        for path, unit in items_iter:
            try:
                orig_path_u = to_unicode_safe(path)
                # 检测是否为高度混淆或非 ASCII 路径
                has_non_ascii = any(ord(c) < 32 or ord(c) > 126 for c in orig_path_u)

                if (base_name == "assets" and (has_non_ascii or len(orig_path_u) > 100)) or (has_non_ascii and len(orig_path_u) > 150):
                    # 扁平化哈希映射模式 (Flattened Hash Mode)
                    ext = ""
                    if "." in orig_path_u and not orig_path_u.endswith("."):
                        ext = "." + orig_path_u.split(".")[-1][:10]
                    if not ext:
                        ext = ".bin"

                    safe_hash = hashlib.md5(orig_path_u.encode("utf-8")).hexdigest()[:8] if hasattr(hashlib, "md5") else str(hash(orig_path_u))[:8]
                    flat_name = "asset_" + str(curr_success + 1).zfill(3) + "_" + safe_hash + ext
                    full_path = os.path.join(target_base, flat_name)
                    manifest_map[flat_name] = {
                        "original_path": orig_path_u,
                        "exported_file": flat_name,
                    }
                else:
                    # 常规树状结构导出模式
                    rel_clean = orig_path_u.replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)
                    if len(rel_clean) >= 2 and rel_clean[1] == ":":
                        rel_clean = rel_clean[2:].lstrip(os.sep)
                    rel_clean = rel_clean.replace("..", "_")

                    prefix_pattern = base_name.replace("/", os.sep).replace("\\", os.sep) + os.sep
                    if rel_clean.startswith(prefix_pattern):
                        rel_clean = rel_clean[len(prefix_pattern):]

                    clean_chars = []
                    for c in rel_clean:
                        if c in '<>:"|?*' or ord(c) < 32 or ord(c) > 126:
                            clean_chars.append('_')
                        else:
                            clean_chars.append(c)
                    rel_clean = "".join(clean_chars)
                    while "__" in rel_clean:
                        rel_clean = rel_clean.replace("__", "_")

                    rel_clean = rel_clean.strip("_")
                    if not rel_clean:
                        rel_clean = "file_" + str(curr_success + 1) + ".dat"

                    full_path = os.path.abspath(os.path.join(target_base, rel_clean))
                    if os.path.isdir(full_path) or full_path.endswith(os.sep):
                        full_path = os.path.join(full_path, "data_" + str(curr_success + 1) + ".bin")

                # 防路径穿越断言校验
                if not (full_path == target_base or full_path.startswith(target_base + os.sep)):
                    continue

                p_dir = os.path.dirname(full_path)
                if not os.path.exists(p_dir):
                    os.makedirs(p_dir)

                has_streamed = False
                if FileOutputStream and hasattr(unit, "getInput"):
                    inp = unit.getInput()
                    if inp:
                        # 1. 优先尝试原生 Stream 缓冲写入
                        try:
                            stream = inp.getStream()
                            if stream:
                                fos = FileOutputStream(full_path)
                                buffer = zeros(16384, "b")
                                while True:
                                    read_bytes = stream.read(buffer)
                                    if read_bytes <= 0:
                                        break
                                    fos.write(buffer, 0, read_bytes)
                                fos.close()
                                stream.close()
                                has_streamed = True
                                curr_success += 1
                        except (Exception, JavaThrowable):
                            pass

                        # 2. Stream 失败时，使用 IInput 原生 getBytes(0, size) 兜底直读取写
                        if not has_streamed:
                            try:
                                sz = inp.getSize() if hasattr(inp, "getSize") else 0
                                if sz > 0:
                                    raw_b = inp.getBytes(0, int(sz))
                                    if raw_b:
                                        with open(full_path, "wb") as f:
                                            f.write(raw_b)
                                        has_streamed = True
                                        curr_success += 1
                            except (Exception, JavaThrowable):
                                pass

                if has_streamed:
                    continue

                content = _extract_text_content(unit)
                if not content:
                    if hasattr(unit, "getBytes"):
                        try:
                            content = unit.getBytes()
                        except (Exception, JavaThrowable):
                            pass
                    elif hasattr(unit, "getData"):
                        try:
                            content = unit.getData()
                        except (Exception, JavaThrowable):
                            pass

                # 如果 JEB Unit 内存流和文本提取均无法获取数据，从底层 APK Zip 匹配写入
                if not has_streamed and content is None and filepath and os.path.exists(filepath):
                    try:
                        import zipfile
                        with zipfile.ZipFile(filepath, "r") as zf:
                            for info in zf.infolist():
                                f_u = to_unicode_safe(info.filename)
                                if f_u == orig_path_u or orig_path_u.endswith(f_u) or f_u.endswith(orig_path_u) or (not (f_u.startswith("res/") or f_u == "AndroidManifest.xml" or f_u.endswith(".dex") or f_u.startswith("META-INF/")) and not info.filename.endswith("/")):
                                    with open(full_path, "wb") as f_out:
                                        f_out.write(zf.read(info.filename))
                                    content = "ZIP_WRITTEN"
                                    break
                    except Exception:
                        pass

                if content is not None:
                    if content != "ZIP_WRITTEN":
                        with open(full_path, "wb") as f:
                            if isinstance(content, _unicode_type):
                                f.write(content.encode("utf-8"))
                            elif isinstance(content, (bytes, bytearray)):
                                f.write(content)
                            else:
                                f.write(str(content))
                    curr_success += 1
            except (Exception, JavaThrowable) as exc:
                print(u"[MCP] Warning: save_units failed for path '{0}': {1}".format(to_unicode_safe(path)[:50], to_unicode_safe(exc)))
                pass
        return curr_success

    success += save_units(res_index, "res")
    success += save_units(asset_index, "assets")

    # 写出 assets_manifest.json 映射对照表
    saved_manifest = None
    if manifest_map:
        try:
            manifest_file = os.path.join(output_dir, "assets_manifest.json")
            with open(manifest_file, "wb") as f:
                json_bytes = json.dumps(manifest_map, indent=2).encode("utf-8")
                f.write(json_bytes)
            saved_manifest = "assets_manifest.json"
        except (Exception, JavaThrowable) as e:
            print(u"[MCP] Warning: Failed to write assets_manifest.json: " + to_unicode_safe(e))

    return {
        "status": "success",
        "total": total,
        "exported": success,
        "output_dir": output_dir,
        "manifest": saved_manifest,
    }


def _is_platform_type(signature):
    """判断签名是否属于 Android/Java/Dalvik/Kotlin 平台类型"""
    return (
        signature.startswith("Ldalvik")
        or signature.startswith("Ljava")
        or signature.startswith("Landroid")
        or signature.startswith("Lkotlin")
    )


def raise_class_not_found(class_signature):
    if _is_platform_type(class_signature):
        raise JSONRPCError(-1, ErrorMessages.CLASS_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.CLASS_NOT_FOUND)


def raise_method_not_found(method_signature):
    if _is_platform_type(method_signature):
        raise JSONRPCError(-1, ErrorMessages.METHOD_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.METHOD_NOT_FOUND)


def raise_field_not_found(field_signature):
    if _is_platform_type(field_signature):
        raise JSONRPCError(-1, ErrorMessages.FIELD_NOT_FOUND_WITHOUT_CHECK)
    raise JSONRPCError(-1, ErrorMessages.FIELD_NOT_FOUND)


CTX = None

# ---------------------------------------------------------------------------
# 热重载支持
# ---------------------------------------------------------------------------
# 核心问题：JEB 每次 Run Script 时, Jython 会重新执行整个模块, 产生新的
# rpc_registry / Server 实例。但旧的 HTTP Server 线程仍在运行并占用端口。
#
# 解决方案：利用 Java 的 System Properties 在 JVM 级别保存旧 Server 的引用。
# 脚本被重新执行时, 可以通过同一个 property key 找到并关闭旧 Server, 然后
# 启动携带最新代码的新 Server，无需重启 JEB。
# ---------------------------------------------------------------------------


_MCP_SERVER_PROP_KEY = "__jeb_mcp_server_instance__"


def _stop_previous_server():
    """
    尝试关闭上一次脚本运行时遗留的 HTTP Server。
    通过 Java System Properties 存取跨模块加载的 Server 引用。
    """
    try:
        if JavaSystem:
            old_server = JavaSystem.getProperties().get(_MCP_SERVER_PROP_KEY)
            if old_server is not None:
                print("[MCP] Hot-reload: stopping previous server...")
                old_server.stop()
                JavaSystem.getProperties().remove(_MCP_SERVER_PROP_KEY)
                # 给操作系统一点时间释放端口
                time.sleep(0.3)
                print("[MCP] Hot-reload: previous server stopped.")
    except (Exception, JavaThrowable) as e:
        print(u"[MCP] Hot-reload: failed to stop previous server: {0}".format(to_unicode_safe(e)))


def _save_server_reference(server):
    """将当前 Server 实例保存到 Java System Properties 中，供下次热重载使用。"""
    if JavaSystem:
        JavaSystem.getProperties().put(_MCP_SERVER_PROP_KEY, server)


class MCP(IScript):
    def __init__(self):
        self.server = None
        print(u"[MCP] Plugin loaded")

    def run(self, ctx):
        global CTX
        CTX = ctx

        # 1. 关闭上一次运行遗留的旧 Server (热重载核心)
        _stop_previous_server()

        # 2. 清理缓存确保状态一致
        clear_apk_cache()
        clearArtifactQueue()

        # 3. 启动新 Server (此时 rpc_registry 已包含最新的函数定义)
        self.server = Server()
        self.server.start()
        _save_server_reference(self.server)
        print(u"[MCP] Plugin running (hot-reload ready)")

        is_daemon = int(os.getenv("JEB_MCP_DAEMON", "0"))
        if is_daemon == 1:
            try:
                while True:
                    time.sleep(10)
            except KeyboardInterrupt:
                print("Exiting...")

    def term(self):
        if self.server:
            self.server.stop()
        if JavaSystem:
            JavaSystem.getProperties().remove(_MCP_SERVER_PROP_KEY)
