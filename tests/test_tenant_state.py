"""掃整個後端，找出沒有按店家分開的行程內狀態。

一個服務同時服務好幾家店之後，任何「存在記憶體、所有請求共用」的東西，
key 裡沒有店家就會讓 A 店的資料被 B 店的請求讀到（理由見 tenancy.py 開頭）。
這個測試把「記得要按店家分開」變成「忘了就會失敗」。

會被抓的寫法（模組層，也就是整個行程共用的）：

    A. 函式裡用 global 重新指派模組層變數            global _cache
    B. 原地修改模組層的 dict/list/set/deque…          _cache[key] = ...
    C. functools.lru_cache / cache 這類函式快取       @lru_cache
    D. 模組層單例的 self.xxx 狀態                     pool = _Pool()；self._seen[k] = ...

要存狀態請用 tenancy.PerTenant / tenancy.TableCache。

★ 真的不該按店家分開的（例如寄信的背景佇列：每封信自帶店家與寄件人），
  加進 ALLOWED，**一定要寫理由**。理由寫不出來，就代表它其實該分開。
  ALLOWED 裡的項目如果已經不存在，測試也會失敗，避免清單過期。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (檔案相對路徑, 名稱) -> 為什麼這個可以全行程共用
ALLOWED: dict[tuple[str, str], str] = {
    ("tenancy.py", "_registry"): (
        "店家清單本身。第一次用到時讀一次環境變數，之後唯讀；它就是「有哪些店」，"
        "本來就是全行程一份。"
    ),
    ("notifications.py", "_outbox"): (
        "寄信的背景佇列。每封 Message 在請求裡就組好（含店家、寄件人、收件人），"
        "背景執行緒只負責送出，不讀任何店家狀態；所有店共用一個 Brevo 帳號。"
    ),
    ("notifications.py", "_worker"): "同上，送信的那一條背景執行緒。",
    ("line_utils/client.py", "_outbox"): (
        "LINE 推播的背景佇列。每一則在排隊時就帶上那家店的 access token 與店家識別，"
        "背景執行緒不讀任何店家狀態。"
    ),
    ("line_utils/client.py", "_worker"): "同上，推播的那一條背景執行緒。",
}

_MUTABLE_CALLS = {
    "dict", "list", "set", "deque", "defaultdict", "OrderedDict", "Counter",
    "Queue", "SimpleQueue", "LifoQueue", "PriorityQueue", "bytearray", "WeakValueDictionary",
    "WeakKeyDictionary",
}
_MUTATING_METHODS = {
    "append", "appendleft", "extend", "extendleft", "insert", "pop", "popleft",
    "popitem", "remove", "clear", "update", "setdefault", "add", "discard",
    "put", "put_nowait", "sort", "reverse",
}
_CACHE_DECORATORS = {"lru_cache", "cache", "cached_property"}


def _python_files() -> list[Path]:
    skip = {"venv", ".venv", "tests", "__pycache__"}
    return sorted(
        p for p in ROOT.rglob("*.py")
        if not (set(p.relative_to(ROOT).parts) & skip)
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    return None


def _is_mutable_value(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)):
        return True
    return _call_name(node) in _MUTABLE_CALLS


def _base_name(node: ast.AST) -> str | None:
    """x / x[k] / x.attr / x[k].attr[j] → x"""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _self_attr(node: ast.AST) -> str | None:
    """self.attr / self.attr[k] / self.attr.x → attr"""
    while isinstance(node, ast.Subscript):
        node = node.value
    while isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Attribute, ast.Subscript)):
        node = node.value
        while isinstance(node, ast.Subscript):
            node = node.value
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        return node.attr
    return None


def _module_mutables(tree: ast.Module) -> set[str]:
    names = set()
    for stmt in tree.body:
        targets, value = [], None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        if _is_mutable_value(value):
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _local_names(func: ast.AST) -> set[str]:
    """函式自己的參數與區域變數——同名的不算動到模組層。"""
    names = set()
    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = func.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None:
                names.add(arg.arg)
    declared_global = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Global):
            declared_global |= set(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names - declared_global


def _mutations_of(tree: ast.Module, watched: set[str]) -> list[tuple[str, int]]:
    """模組裡原地修改 watched 這些名稱的地方。"""
    found = []
    scopes: list[tuple[ast.AST, set[str]]] = [(tree, set())]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            scopes.append((node, _local_names(node)))

    def shadowed(name: str, lineno: int) -> bool:
        for scope, local in scopes:
            if scope is tree:
                continue
            start = getattr(scope, "lineno", 0)
            end = getattr(scope, "end_lineno", 0)
            if start <= lineno <= end and name in local:
                return True
        return False

    for node in ast.walk(tree):
        name = None
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) or (
                    isinstance(node, ast.AugAssign) and isinstance(t, (ast.Subscript, ast.Attribute))
                ):
                    name = _base_name(t)
                elif isinstance(t, ast.Attribute):
                    name = _base_name(t)
                if name in watched and not shadowed(name, node.lineno):
                    found.append((name, node.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _MUTATING_METHODS:
                name = _base_name(node.func.value)
                if name in watched and not shadowed(name, node.lineno):
                    found.append((name, node.lineno))
    return found


def _singleton_state(tree: ast.Module) -> list[tuple[str, int]]:
    """模組層建立的、同一個檔案裡定義的類別實例，方法裡有沒有改 self 的狀態。"""
    classes = {s.name: s for s in tree.body if isinstance(s, ast.ClassDef)}
    instantiated = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            name = _call_name(stmt.value) if stmt.value is not None else None
            if name in classes:
                instantiated.add(name)
    found = []
    for cname in instantiated:
        for method in classes[cname].body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(method):
                attr = None
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        attr = _self_attr(t)
                        if attr and method.name != "__init__":
                            found.append((f"{cname}.{attr}", node.lineno))
                        elif attr and isinstance(t, ast.Subscript):
                            found.append((f"{cname}.{attr}", node.lineno))
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in _MUTATING_METHODS):
                    attr = _self_attr(node.func.value)
                    if attr:
                        found.append((f"{cname}.{attr}", node.lineno))
    return found


def _env_reads(tree: ast.Module) -> list[tuple[str, int]]:
    """直接從環境變數讀每店設定的地方（os.environ[...] / .get / os.getenv）。

    ★ 每店設定只能經過 tenancy 讀。直接讀 DATABASE_URL 的程式在單店時
      剛好讀到對的，多店家時讀到的是空的（或更糟：讀到某一家的）。
    """
    found = []
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            key = node.slice
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and _is_environ(node.func.value) and node.args:
                key = node.args[0]
            elif node.func.attr == "getenv" and node.args:
                key = node.args[0]
        if isinstance(key, ast.Constant) and key.value in _per_tenant_keys():
            found.append((f"os.environ[{key.value}]", node.lineno))
    return found


def _per_tenant_keys() -> set[str]:
    """tenancy.PER_TENANT_KEYS。直接從原始碼讀，不 import——import tenancy
    會讀環境變數裡的店家設定，掃描器不該依賴那個。"""
    tree = ast.parse((Path(__file__).resolve().parent.parent / "tenancy.py").read_text("utf-8"))
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and getattr(stmt.targets[0], "id", None) == "PER_TENANT_KEYS"):
            return {e.value for e in stmt.value.elts}
    raise AssertionError("tenancy.py 裡找不到 PER_TENANT_KEYS")


def _is_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def scan() -> dict[tuple[str, str], list[int]]:
    """回傳 {(檔案, 名稱): [行號...]}，只含沒在 ALLOWED 裡的。"""
    problems: dict[tuple[str, str], list[int]] = {}

    def add(rel: str, name: str, line: int) -> None:
        problems.setdefault((rel, name), []).append(line)

    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)

        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                for name in node.names:
                    add(rel, name, node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    target = deco.func if isinstance(deco, ast.Call) else deco
                    dname = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                    if dname in _CACHE_DECORATORS:
                        add(rel, f"@{dname} {node.name}", node.lineno)

        for name, line in _mutations_of(tree, _module_mutables(tree)):
            add(rel, name, line)
        for name, line in _singleton_state(tree):
            add(rel, name, line)
        if rel != "tenancy.py":
            for name, line in _env_reads(tree):
                add(rel, name, line)

    return {k: v for k, v in problems.items() if k not in ALLOWED}


def test_no_unscoped_process_state():
    problems = scan()
    report = "\n".join(
        f"  {rel}: {name}（第 {', '.join(map(str, sorted(set(lines))))} 行）"
        for (rel, name), lines in sorted(problems.items())
    )
    assert not problems, (
        "以下是整個行程共用、卻沒有按店家分開的狀態。改用 tenancy.PerTenant / "
        "tenancy.TableCache；真的不該分開的，加進 ALLOWED 並寫理由：\n" + report
    )


def test_allowlist_is_not_stale():
    """ALLOWED 裡的每一項都要還存在，不然清單會慢慢變成沒人懂的歷史。"""
    present = set()
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                present |= {(rel, n) for n in node.names}
        present |= {(rel, n) for n, _ in _mutations_of(tree, _module_mutables(tree))}
    stale = [k for k in ALLOWED if k not in present]
    assert not stale, f"ALLOWED 裡這些已經不存在了，請刪掉：{stale}"


def test_scanner_catches_known_shapes(tmp_path, monkeypatch):
    """掃描器自己的測試：每一種該抓的寫法都要抓得到，不然它會靜靜地放過。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import functools\n"
        "from collections import deque\n"
        "_cache = {}\n"
        "_hits: dict = dict()\n"
        "_q = deque()\n"
        "LABELS = {'a': 1}\n"           # 唯讀常數：不該抓
        "_flag = False\n"
        "def f(k):\n"
        "    global _flag\n"
        "    _flag = True\n"
        "    _cache[k] = 1\n"
        "    _hits.setdefault(k, 0)\n"
        "    _q.append(k)\n"
        "    return LABELS['a']\n"
        "def g(_cache):\n"              # 參數同名：不是模組層那個
        "    _cache[1] = 2\n"
        "@functools.lru_cache\n"
        "def h(x):\n"
        "    return x\n"
        "class _S:\n"
        "    def __init__(self):\n"
        "        self._seen = {}\n"
        "    def mark(self, k):\n"
        "        self._seen[k] = 1\n"
        "single = _S()\n"
        "import os\n"
        "URL = os.environ['DATABASE_URL']\n"
        "TOKEN = os.getenv('JOB_TOKEN')\n"
        "TTL = os.environ.get('SOME_TTL', '3')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    found = scan()
    names = {name for (_rel, name) in found}
    assert {"_flag", "_cache", "_hits", "_q", "@lru_cache h", "_S._seen",
            "os.environ[DATABASE_URL]", "os.environ[JOB_TOKEN]"} <= names
    assert "os.environ[SOME_TTL]" not in names, "不是每店設定的環境變數不該抓"
    assert "LABELS" not in names
    assert found[("sample.py", "_cache")] == [11], "參數同名的那一行不該算"
