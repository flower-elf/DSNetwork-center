"""测 release 资产契约：便携 ZIP 按 release/portable.txt 显式清单打包、携带能启动网关的裁剪运行时，release 只允许 release job 写入且打标签前先核对 push 测试结果。

运行：python tests/_test_release_assets.py
"""
import ast
import glob
import hashlib
import os
import re
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "release"))

import matrix_legs  # noqa: E402
import portable_runtime  # noqa: E402
import release_tools as tools  # noqa: E402

# 发布要求仓库持续运行的腿。gate 从 tests.yml 推导它们，
# 这个元组用于核对推导结果。
EXPECTED_LEGS = (
    "ubuntu-latest / python 3.9",
    "ubuntu-latest / python 3.12",
    "windows-latest / python 3.12",
)

# Windows CPython 把这些模块静态链接进 python312.dll，包内运行时没有对应的 Lib
# 文件可要求。Linux 构建把 math 做成共享模块，所以宿主机的
# sys.builtin_module_names 代表不了 Windows 运行时实际内置的模块。
WINDOWS_BUILTINS = {"math"}


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def make_runtime(directory, extra=()):
    """伪造一个能通过 portable_runtime.verify() 的运行时目录。"""
    for rel in list(portable_runtime.REQUIRED_FILES) + list(extra):
        path = os.path.join(directory, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"stub:" + rel.encode("utf-8"))
    return directory


# ---- release 写入扫描器 ----------------------------------------------------
#
# release 写入命令指工作流真正执行的命令：处在命令位置，即 run: | 块内的独立行、
# 单行 run: 之后，或 shell 分隔符之后。echo 里打印的同名命令不算——命令位置区分
# 「真正执行」与「只是提到」。
#
# gh api 是难点：默认 GET，但出现任一带值 flag 就转成 POST，所以
# `gh api repos/o/r/releases -f tag_name=v1` 不写方法名也会创建 release。
# 只有显式 `-X GET` / `--method GET` 才算读取。

# `gh release <verb>`：会改动 release 的四个动词。
GH_RELEASE_WRITE = re.compile(r"^gh\s+release\s+(?:create|edit|upload|delete)\b")
GH_API = re.compile(r"^gh\s+api\b")
# 匹配 `-X POST`、`-XPOST`、`--method POST`、`--method=POST`。
API_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)(?:[=\s]*)([A-Za-z]+)")
# 会让 gh 发送请求体的 flag，它们把默认方法翻成 POST。
API_BODY_FLAG = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:=|\s|$)")

# 切分 shell 命令的分隔符。刻意不切 `|`：切它会把引号里的字符串劈开，这里没有收益。
SHELL_SEPARATOR = re.compile(r"&&|\|\||;")
# 单行 step 的命令跟在 `run:` 之后，也可能写成列表项。
INLINE_RUN = re.compile(r"^(?:-\s*)?run:\s*")


def shell_commands(text):
    """工作流里的逻辑 shell 命令，把 `\\` 续行拼成一条。

    只做续行归并，不解析 shell 语法。
    """
    joined, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        joined.append(buf + line)
        buf = ""
    if buf:
        joined.append(buf)

    for line in joined:
        for piece in SHELL_SEPARATOR.split(line):
            piece = INLINE_RUN.sub("", piece.strip())
            if piece:
                yield piece


def is_release_mutation(command):
    """执行这条命令会不会改动 GitHub Release。"""
    if GH_RELEASE_WRITE.match(command):
        return True
    if not GH_API.match(command):
        return False
    method = API_METHOD.search(command)
    if method:
        return method.group(1).upper() != "GET"
    return bool(API_BODY_FLAG.search(command))


def release_mutations(text):
    """一段工作流文本中会改动 release 的命令。"""
    return [c for c in shell_commands(text) if is_release_mutation(c)]


def workflow_jobs(text):
    """工作流顶层 `jobs:` 映射的 {job id: job 文本}。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            start = i
            break
    if start is None:
        return {}
    jobs, current, buf = {}, None, []
    for line in lines[start + 1:]:
        if not line.strip():
            if current:
                buf.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and not line.lstrip().startswith("#") and line.rstrip().endswith(":"):
            if current:
                jobs[current] = "\n".join(buf)
            current = line.strip()[:-1]
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        jobs[current] = "\n".join(buf)
    return jobs


def dispatch_input_names(text):
    """`on.workflow_dispatch.inputs` 下声明的输入名。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "workflow_dispatch:":
            continue
        parent = len(line) - len(line.lstrip())
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if not cur.strip() or cur.lstrip().startswith("#"):
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= parent:
                break
            if cur.strip() != "inputs:":
                continue
            names = []
            for nxt in lines[j + 1:]:
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    continue
                nind = len(nxt) - len(nxt.lstrip())
                if nind <= indent:
                    break
                if nind == indent + 2 and nxt.strip().endswith(":"):
                    names.append(nxt.strip()[:-1])
            return names
    return []


def release_writer_jobs(text):
    """至少执行一条 release 写入命令的 job。"""
    return sorted(name for name, body in workflow_jobs(text).items()
                  if release_mutations(body))


# 唯一允许写 Release 的 job，也是 dispatch 唯一的输入。tests 在每次 push 到 main
# 时运行，写入方核对那次结果，不在 release 运行里重复测试矩阵。
RELEASE_WRITER_JOB = "release"
DISPATCH_INPUTS = ["version"]


def check_release_writers(text):
    """release 契约不成立时抛 AssertionError。

    本 fork 走手动 dispatch 发布，没有 tag push 触发器。安全要求是：唯一的写入
    job 持有 `contents: write`，打标签前先读取同一提交的 push 测试运行结果，
    且 dispatch 只提供 version 一个输入。
    """
    jobs = workflow_jobs(text)
    writers = release_writer_jobs(text)
    assert writers, "没找到任何 release 写入点——检查器本身失效了，不是工作流变干净了"
    assert writers == [RELEASE_WRITER_JOB], (
        "只有 %s 这一个 job 可以写 release，实际是 %s"
        % (RELEASE_WRITER_JOB, writers))
    body = jobs[RELEASE_WRITER_JOB]
    assert "contents: write" in body, (
        "release job 需要 contents: write 才能打标签、建 Release")
    assert "actions: read" in body, (
        "release job 需要 actions: read 才能读取测试运行结果")
    assert 'gh run list --workflow tests.yml --commit "$SHA" --event push' in body, (
        "release job 必须找到同一提交的 push 测试运行")
    assert 'gh run watch "$RUN_ID" --exit-status' in body, (
        "release job 必须等测试运行结束，并以它的结果决定是否继续")
    assert body.index("Wait for the push tests") < body.index("Tag and publish"), (
        "测试结果核对必须在打标签之前")
    assert "uses: ./.github/workflows/tests.yml" not in text, (
        "release 不应重复运行测试矩阵；直接读取 push 触发的那次结果")
    assert not re.search(r"(?m)^\s*needs:\s*test\s*$", text), (
        "release 不应依赖测试 job；测试由 push 触发，结果单独读取")
    inputs = dispatch_input_names(text)
    assert inputs == DISPATCH_INPUTS, (
        "workflow_dispatch 只能有 version 一个输入，实际是 %s" % inputs)


class ManifestTests(unittest.TestCase):
    """release/portable.txt 是打包文件的权威清单。"""

    def test_every_listed_path_exists(self):
        for name in tools.read_manifest():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)),
                            "清单里的 %s 不存在" % name)

    def test_every_application_file_is_listed(self):
        # 新模块或 app/web 下的仪表盘文件若忘了登记，便携包会缺文件，
        # 用户机器上启动即 ModuleNotFoundError 或面板空白。
        on_disk = []
        for base, dirs, files in os.walk(os.path.join(ROOT, "app")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), ROOT)
                on_disk.append(rel.replace(os.sep, "/"))
        self.assertTrue(on_disk, "仓库里没有 app/ 源码？")
        listed = set(tools.read_manifest())
        self.assertEqual(sorted(p for p in on_disk if p not in listed), [])

    def test_launchers_and_gateway_are_listed(self):
        listed = tools.read_manifest()
        for name in ("app/wb_proxy.py", "app/web/index.html", "LICENSE",
                     "start-wb-proxy.bat", "start-wb-proxy.sh",
                     "start-wb-proxy.command", "start-wb-proxy-lan.sh"):
            self.assertIn(name, listed)

    def test_every_launcher_script_is_listed(self):
        """启动脚本按前缀全量扫：漏登记一个就在这儿红。

        quick-deploy.sh 是 Docker 路径、刻意不进便携包，不在前缀里。
        """
        listed = tools.read_manifest()
        for prefix in ("start-wb-proxy", "allow-firewall"):
            for path in sorted(glob.glob(os.path.join(ROOT, prefix + "*"))):
                name = os.path.basename(path)
                if name.endswith((".bat", ".sh", ".command")):
                    self.assertIn(name, listed, "便携包清单缺少 %s" % name)

    def test_removed_restart_scripts_stay_removed(self):
        """重启脚本与无窗口启动已经取消，同步上游时不要再带回来。"""
        leftovers = sorted(glob.glob(os.path.join(ROOT, "restart-wb-proxy*")))
        self.assertEqual(leftovers, [])
        for name in ("start-wb-proxy.bat", "start-wb-proxy-lan.bat",
                     "start-wb-proxy.sh", "start-wb-proxy-lan.sh"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("pythonw", text.lower(), name)
            self.assertNotIn("console_tail", text, name)

    def test_runtime_data_files_are_listed(self):
        # wb_pricing._candidate_file() 优先读 pricing/pricing.json，缺了它，
        # 包里带的会是模块内联的旧价格。
        self.assertIn("pricing/pricing.json", tools.read_manifest())

    def test_entries_are_relative_and_inside_the_repo(self):
        for name in tools.read_manifest():
            self.assertFalse(os.path.isabs(name), name)
            self.assertNotIn("..", name.split("/"), name)
            self.assertNotIn("\\", name, name)

    def test_no_duplicates(self):
        listed = tools.read_manifest()
        self.assertEqual(len(listed), len(set(listed)))

    def test_user_data_directories_are_not_managed_files(self):
        # 更新器只替换受管文件，必须原样保留 accounts/ 与 usage/，
        # 清单里不能有落在它们内部的条目。
        for name in tools.read_manifest():
            for protected in tools.PROTECTED:
                self.assertFalse(name.startswith(protected),
                                 "%s 落在受保护目录 %s 里" % (name, protected))

    def test_development_only_files_stay_out(self):
        listed = tools.read_manifest()
        for name in ("Dockerfile", "docker-compose.yml", "quick-deploy.sh",
                     "tests/run_all.py"):
            self.assertNotIn(name, listed)


class RuntimeTrimTests(unittest.TestCase):
    """裁剪规则要精确复现便携包实际携带的运行时。"""

    def test_the_app_modules_survive_the_trim(self):
        # 网关导入的每个标准库模块都必须进契约清单，
        # 否则裁剪可能删掉它依赖的模块。
        imported = set()
        for path in glob.glob(os.path.join(ROOT, "app", "wb_*.py")):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        local = {os.path.basename(p)[:-3]
                 for p in glob.glob(os.path.join(ROOT, "app", "*.py"))}
        # sys、time 等编译进 python312.dll，裁剪删不掉它们，也没有 Lib/ 文件可查。
        needed = sorted(m for m in imported
                        if m not in local and m not in sys.builtin_module_names
                        and m not in WINDOWS_BUILTINS)
        self.assertTrue(needed)
        covered = set(portable_runtime.REQUIRED_FILES)
        missing = [m for m in needed
                   if ("Lib/%s.py" % m) not in covered
                   and ("Lib/%s/__init__.py" % m) not in covered]
        self.assertEqual(missing, [], "这些导入没有出现在运行时常量表里：%s" % missing)

    def test_the_documented_trim_is_exactly_what_goes(self):
        for rel in ("Lib/multiprocessing", "Lib/tkinter/__init__.py", "Lib/idlelib/idle.py",
                    "Lib/site-packages/pip/__init__.py", "Lib/venv/__init__.py",
                    "Scripts/pip.exe", "include/Python.h", "libs/python312.lib",
                    "tcl/tk8.6/init.tcl", "pythonw.exe", "python312.pdb",
                    "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
                    "DLLs/zlib1.dll", "DLLs/_testcapi.pyd", "Lib/__pycache__/os.cpython-312.pyc"):
            self.assertFalse(portable_runtime.keep(rel), "%s 应该被裁掉" % rel)

    def test_what_the_launchers_need_survives(self):
        for rel in ("python.exe", "python3.dll", "python312.dll", "vcruntime140.dll",
                    "LICENSE.txt", "Lib/os.py", "Lib/ssl.py", "Lib/sqlite3/__init__.py",
                    "DLLs/_socket.pyd", "DLLs/_ssl.pyd", "DLLs/libssl-3-x64.dll",
                    "DLLs/_ctypes.pyd", "DLLs/libffi-8.dll"):
            self.assertTrue(portable_runtime.keep(rel), "%s 不能被裁掉" % rel)

    def test_verify_rejects_a_runtime_that_cannot_start_the_gateway(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory)
            portable_runtime.verify(directory)          # 这个夹具是合法的

            os.remove(os.path.join(directory, "python.exe"))
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)

    def test_verify_rejects_trimmed_files_coming_back(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory)
            os.makedirs(os.path.join(directory, "Lib", "multiprocessing"))
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)

    def test_verify_rejects_stray_debug_symbols(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory, extra=["DLLs/_socket.pdb"])
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)


class PortableZipTests(unittest.TestCase):
    # 版本从当前检出的源码读取。版本号更新后工具必须照常构建，
    # 测试若写死旧版本号会掩盖问题。
    VERSION = tools.source_version(ROOT)
    TAG = "v" + VERSION

    def build(self, directory, name="out.zip", runtime=None, **kwargs):
        runtime = runtime or make_runtime(os.path.join(directory, "python"))
        return tools.build_portable(self.VERSION, self.TAG,
                                    os.path.join(directory, name),
                                    source=ROOT, runtime_dir=runtime, **kwargs)

    def test_archive_holds_the_manifest_the_runtime_and_the_marker(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            root = tools.PACKAGE_ROOT
            self.assertEqual(sorted(n for n in names if not n.startswith(root + "/")),
                             [tools.MANIFEST_NAME])
            self.assertEqual(sorted(n[len(root) + 1:] for n in names if n.startswith(root + "/")
                                    and not n.startswith(root + "/python/")),
                             sorted(files))
            self.assertIn("%s/python/python.exe" % root, names)
            self.assertIn("%s/python/Lib/os.py" % root, names)

    def test_the_archive_is_not_a_source_archive(self):
        # 不带运行时的 ZIP 起不了网关，构建器不许产出这种产物。
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            runtime_files = [n for n in zipfile.ZipFile(path).namelist()
                             if n.endswith("python/python.exe")]
            self.assertEqual(len(runtime_files), 1)

    def test_dot_paths_are_never_packaged(self):
        self.assertTrue(tools.is_dot_path(".github/workflows/x.yml"))
        self.assertTrue(tools.is_dot_path("app/.cache/x"))
        self.assertFalse(tools.is_dot_path("app/web/index.html"))
        self.assertFalse(tools.is_dot_path("dotfiles/README.md"))
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            runtime = make_runtime(os.path.join(directory, "python"),
                                   extra=(".hidden/secret.txt",))
            path, _ = self.build(directory, runtime=runtime)
            names = zipfile.ZipFile(path).namelist()
            self.assertFalse([n for n in names if ".hidden" in n], names)

    def test_a_missing_runtime_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            with self.assertRaises(SystemExit):
                tools.build_portable(self.VERSION, self.TAG,
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=None)
            with self.assertRaises(SystemExit):
                tools.build_portable(self.VERSION, self.TAG,
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=os.path.join(directory, "nope"))

    def test_a_runtime_that_fails_the_contract_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            bad = make_runtime(os.path.join(directory, "bad"))
            os.remove(os.path.join(bad, "python.exe"))
            with self.assertRaises(SystemExit):
                tools.build_portable(self.VERSION, self.TAG,
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=bad)

    def test_embedded_manifest_describes_the_release(self):
        import json
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(tools.MANIFEST_NAME).decode("utf-8"))
            self.assertEqual(manifest["files"], files)
            self.assertEqual(manifest["protected"], tools.PROTECTED)
            self.assertEqual(manifest["root"], tools.PACKAGE_ROOT)
            self.assertEqual(manifest["version"], tools.source_version(ROOT))
            self.assertEqual(manifest["tag"], self.TAG)
            # 更新器把运行时当作一棵子树整体替换，manifest 必须写明它，不能留作隐含信息。
            self.assertEqual(manifest["runtime"]["prefix"], "python/")
            self.assertEqual(manifest["runtime"]["python"], portable_runtime.RUNTIME_VERSION)
            self.assertGreater(manifest["runtime"]["files"], 0)

    def test_the_launchers_look_where_the_runtime_is(self):
        # 打包路径与启动脚本探测的路径必须一致，否则包会悄悄回退到系统解释器。
        bat = read("start-wb-proxy.bat")
        self.assertIn("python\\python.exe", bat)
        self.assertIn("app\\wb_proxy.py", bat)
        sh = read("start-wb-proxy.sh")
        self.assertIn("python/bin/python3", sh)
        self.assertIn("app/wb_proxy.py", sh)

    def test_the_archive_root_matches_the_package_layout(self):
        # 解压产物只含一个顶层目录，启动脚本与更新路径据此保持一致。
        self.assertEqual(tools.PACKAGE_ROOT, "wb-proxy-center")

    def test_archive_paths_are_safe(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    self.assertFalse(name.startswith("/"), name)
                    self.assertNotIn("..", name.split("/"), name)

    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            runtime = make_runtime(os.path.join(directory, "python"))
            first, _ = self.build(directory, "a.zip", runtime=runtime)
            second, _ = self.build(directory, "b.zip", runtime=runtime)
            with open(first, "rb") as fa, open(second, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            runtime = make_runtime(os.path.join(directory, "python"))
            with self.assertRaises(SystemExit):
                tools.build_portable("9.9.9", "v9.9.9",
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=runtime)


class ChecksumTests(unittest.TestCase):
    def test_covers_every_asset_once_in_sha256sum_format(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            names = [tools.asset_names("0.2.0"), tools.asset_names("0.2.0-rc.1")]
            assets = []
            for i, name in enumerate(names):
                p = os.path.join(directory, name)
                with open(p, "wb") as fh:
                    fh.write(b"asset-%d" % i)
                assets.append(p)
            out = os.path.join(directory, "checksums.txt")
            lines = tools.write_checksums(assets, out)
            self.assertEqual(len(lines), len(names))
            for line, path in zip(lines, sorted(assets, key=os.path.basename)):
                digest, name = line.split("  ")
                self.assertEqual(len(digest), 64)
                self.assertEqual(name.strip(), os.path.basename(path))
                with open(path, "rb") as fh:
                    self.assertEqual(digest, hashlib.sha256(fh.read()).hexdigest())

    def test_hash_follows_the_bytes(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            p = os.path.join(directory, "a.zip")
            with open(p, "wb") as fh:
                fh.write(b"one")
            before = tools.sha256(p)
            with open(p, "wb") as fh:
                fh.write(b"two")
            self.assertNotEqual(before, tools.sha256(p))


class ReleaseBodyTests(unittest.TestCase):
    block = None

    def setUp(self):
        self.zip_name = tools.asset_names("0.2.0")
        self.block = tools.download_block(self.zip_name)

    def test_first_compose_has_the_marker_and_the_fixed_block(self):
        body = tools.compose_body("", self.block)
        self.assertIn(tools.MARKER, body)
        for expected in ("## 下载说明", "wb-proxy-center-v0.2.0.zip", "checksums.txt"):
            self.assertIn(expected, body)

    def test_maintainer_notes_above_the_marker_survive(self):
        notes = "## 本版要点\n\n- 修了一个真问题\n"
        body = tools.compose_body(notes + "\n" + tools.MARKER + "\n旧块\n", self.block)
        self.assertIn("- 修了一个真问题", body)
        self.assertNotIn("旧块", body)

    def test_recompose_is_idempotent(self):
        once = tools.compose_body("", self.block)
        twice = tools.compose_body(once, self.block)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(tools.MARKER), 1)


class MatrixLegsTests(unittest.TestCase):
    """release gate 明确列出每条腿，不信任汇总结果。"""

    def test_the_repository_matrix_is_the_expected_three(self):
        self.assertEqual(tuple(matrix_legs.legs()), EXPECTED_LEGS)

    def test_dropping_a_leg_narrows_what_the_gate_requires(self):
        # 腿从 tests.yml 推导：那边删掉一条，gate 就少等一条；上面的用例负责把
        # 这类删改变成显式失败，防止悄悄放宽要求。
        text = read(".github", "workflows", "tests.yml")
        without = text.replace('          - os: windows-latest\n            python: "3.12"\n', "")
        self.assertNotEqual(without, text, "tests.yml 里没有找到预期的 windows 腿")
        self.assertEqual(tuple(matrix_legs.legs(without)), EXPECTED_LEGS[:2])

    def test_an_unparseable_matrix_yields_nothing(self):
        # 返回空列表代表 gate 必须拒绝，不能理解成无需检查的腿。
        self.assertEqual(matrix_legs.legs("name: tests\njobs:\n  test:\n    runs-on: x\n"), [])


class ReleaseToolingTests(unittest.TestCase):
    def test_the_tools_survive_a_non_utf8_console(self):
        # Windows 运行器的 stdout 是 cp1252，中文进度输出会让进程中断；
        # 每个工具都固定自己的输出流编码，不依赖控制台。
        for name in ("release/portable_runtime.py", "release/release_tools.py"):
            self.assertIn('reconfigure(encoding="utf-8"', read(*name.split("/")), name)

    def test_the_test_workflow_pins_utf8_for_every_python_process(self):
        self.assertIn('PYTHONUTF8: "1"', read(".github", "workflows", "tests.yml"))

    def test_the_package_job_builds_the_portable_zip(self):
        package = read(".github", "workflows", "package.yml")
        self.assertIn("python release/portable_runtime.py download", package)
        self.assertIn("python release/portable_runtime.py prepare", package)
        self.assertIn("python release/release_tools.py portable", package)
        self.assertIn("runs-on: windows-latest", package)


class ReleaseMutationScannerTests(unittest.TestCase):
    """扫描器要区分的命令形态，以表格列明契约：WRITES 全部命中，READS 全部放过。

    `gh api` 是关键的一半：默认 GET，出现任一带值 flag 即转为 POST。
    """

    WRITES = (
        "gh release create \"$TAG\" --draft",
        "gh release edit \"$TAG\" --draft",
        "gh release upload \"$TAG\" dist/*.zip --clobber",
        "gh release delete \"$TAG\" --yes",
        "gh api repos/o/r/releases -X POST -f tag_name=v1",
        "gh api -XPOST repos/o/r/releases",
        "gh api --method PATCH repos/o/r/releases/1",
        "gh api --method=DELETE repos/o/r/releases/1",
        "gh api repos/o/r/releases -f tag_name=v1",
        "gh api repos/o/r/releases -F tag_name=@body.json",
        "gh api repos/o/r/releases --field tag_name=v1",
        "gh api repos/o/r/releases --raw-field tag_name=v1",
        "gh api repos/o/r/releases --input body.json",
    )
    READS = (
        "gh release view \"$TAG\" --json body",
        "gh release list --limit 5",
        "gh api repos/o/r/releases",
        "gh api -X GET repos/o/r/releases -f per_page=1",
        "gh api --method=GET repos/o/r/releases",
        "gh api --method GET repos/o/r/releases/tags/$TAG",
        "echo \"gh release create $TAG --draft\"",
        "python release/release_tools.py checksums --out dist/checksums.txt",
        "git push --force-with-lease origin main",
    )

    def test_every_writing_shape_is_a_mutation(self):
        for command in self.WRITES:
            self.assertTrue(is_release_mutation(command), command)

    def test_every_read_only_shape_is_not_a_mutation(self):
        for command in self.READS:
            self.assertFalse(is_release_mutation(command), command)

    def test_continuations_are_joined_before_the_decision(self):
        text = ("        run: |\n"
                "          gh api \\\n"
                "            repos/o/r/releases \\\n"
                "            -f tag_name=v1\n")
        found = release_mutations(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].split(),
                         ["gh", "api", "repos/o/r/releases", "-f", "tag_name=v1"])

    def test_a_shell_separator_does_not_hide_a_command(self):
        text = "        run: true && gh release delete \"$TAG\" --yes\n"
        self.assertEqual(release_mutations(text), ["gh release delete \"$TAG\" --yes"])

    def test_an_echo_of_a_command_is_not_a_mutation(self):
        text = "        run: |\n          echo \"gh release create $TAG --draft\"\n"
        self.assertEqual(release_mutations(text), [])

    def test_the_real_workflow_only_writes_from_one_job(self):
        text = read(".github", "workflows", "release.yml")
        self.assertEqual(release_writer_jobs(text), ["release"])
        self.assertTrue(release_mutations(workflow_jobs(text)["release"]))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read(".github", "workflows", "release.yml")
        self.jobs = workflow_jobs(self.text)

    def test_triggers_on_a_manual_dispatch_with_one_version_input(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertEqual(dispatch_input_names(self.text), ["version"])
        self.assertNotIn("\n  push:", self.text)
        dispatch = self.text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("required: true", dispatch)

    def test_the_version_is_normalised_and_validated_before_any_write(self):
        step = self.text.split("- name: Validate version", 1)[1]
        step = step.split("- uses: actions/checkout", 1)[0]
        self.assertIn('V="${VERSION_INPUT#v}"', step)
        self.assertIn(r'^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$', step)
        self.assertIn("版本号格式无效", step)
        self.assertIn("exit 1", step)
        # tag、value 和 pre-release 标志全部来自这一个校验过的变量。
        self.assertIn('echo "value=$V"', step)
        self.assertIn('echo "tag=v$V"', step)
        self.assertIn("prerelease=true", step)
        self.assertIn("prerelease=false", step)
        self.assertLess(self.text.index("Validate version"),
                        self.text.index("Check VERSION matches the input"))

    def test_the_version_must_already_match_main(self):
        # app/wb_proxy.py 是唯一事实来源，VERSION 驱动 server_version 与 /health
        # 载荷。发布只给已通过测试的提交打标签，自身不改版本号、不推送。
        self.assertIn("""grep -c '^VERSION = "' app/wb_proxy.py""", self.text)
        self.assertIn(
            """CURRENT="$(sed -n 's/^VERSION = "\\([^"]*\\)"/\\1/p' app/wb_proxy.py)\"""",
            self.text)
        self.assertIn("main 的 VERSION 是", self.text)
        self.assertIn("先提交版本号并推送", self.text)
        self.assertNotIn("git commit -m \"release: $TAG\"", self.text)
        self.assertNotIn("git push origin HEAD:main", self.text)

    def test_the_tag_is_free_and_created_after_the_test_gate(self):
        self.assertIn("git ls-remote --exit-code --tags origin", self.text)
        self.assertIn("标签 $TAG 已存在", self.text)
        self.assertLess(self.text.index("Wait for the push tests"),
                        self.text.index('git tag -a "$TAG" -m "$TAG"'))
        self.assertIn('git push origin "$TAG"', self.text)

    def test_the_release_is_published_with_generated_notes(self):
        self.assertIn('gh release create "$TAG" --verify-tag --title "$TAG" '
                      '--generate-notes', self.text)
        self.assertIn('if [ "$PRERELEASE" = "true" ]; then', self.text)
        self.assertIn("--prerelease", self.text)
        # 本 fork 直接发布，没有草稿阶段或手动发布步骤，release 不会停留在不可见状态。
        self.assertNotIn("--draft", self.text)

    def test_the_release_job_reads_the_push_test_result(self):
        self.assertNotIn("uses: ./.github/workflows/tests.yml", self.text)
        self.assertNotRegex(self.jobs["release"], r"(?m)^\s*needs:\s*test\s*$")
        self.assertIn('gh run list --workflow tests.yml --commit "$SHA" --event push',
                      self.jobs["release"])
        self.assertIn('gh run watch "$RUN_ID" --exit-status', self.jobs["release"])
        self.assertIn("actions: read", self.jobs["release"])
        self.assertIn("contents: write", self.jobs["release"])
        self.assertLess(self.text.index("Wait for the push tests"),
                        self.text.index("Tag and publish"))

    def test_downstream_jobs_consume_the_created_tag(self):
        self.assertIn("tag: ${{ steps.version.outputs.tag }}", self.jobs["release"])
        image = self.jobs["publish-image"]
        self.assertIn("uses: ./.github/workflows/docker-publish.yml", image)
        self.assertIn("tag: ${{ needs.release.outputs.tag }}", image)
        package = self.jobs["package"]
        self.assertIn("uses: ./.github/workflows/package.yml", package)
        self.assertIn("tag: ${{ needs.release.outputs.tag }}", package)

    def test_only_the_release_job_can_write_a_release(self):
        check_release_writers(self.text)
        self.assertEqual(release_writer_jobs(self.text), [RELEASE_WRITER_JOB])

    # ---- 反面证据：检查器必须有能力报错 ------------------------------------
    #
    # 不会失败的检查器证明不了任何事。下面每个变体都是契约真实可能的退化方式，
    # 由这套用例负责发现。

    def test_the_checker_rejects_a_writer_outside_the_release_job(self):
        mutated = self.text.replace(
            "  release:\n",
            "  sneak:\n    runs-on: ubuntu-latest\n    steps:\n"
            "    - run: gh release delete \"$TAG\" --yes\n\n  release:\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_release_without_the_test_gate(self):
        mutated = self.text.replace(" --event push", "", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_dispatch_input_that_could_gate_a_write(self):
        mutated = self.text.replace(
            "    inputs:\n      version:\n",
            "    inputs:\n      version:\n      dry_run:\n        description: \"x\"\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_the_old_reusable_test_job(self):
        mutated = self.text.replace(
            "  release:\n    runs-on:",
            "  release:\n    needs: test\n    runs-on:", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_an_extra_writer_job_with_continuations(self):
        mutated = self.text.replace(
            "  publish-image:\n",
            "  sneak:\n    runs-on: ubuntu-latest\n    steps:\n    - run: |\n"
            "        gh api \\\n"
            "          \"repos/$GH_REPO/releases\" \\\n"
            "          --field tag_name=\"$TAG\"\n\n  publish-image:\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_accepts_a_read_only_api_call_anywhere(self):
        # 契约的另一半：显式 GET 即使带字段也合法。若误报这类命令，
        # 人们往往会选择削弱检查来绕过它。
        synthetic = (
            "name: synthetic\n"
            "on:\n"
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      version:\n"
            "        description: x\n"
            "        required: true\n"
            "jobs:\n"
            "  release:\n"
            "    runs-on: ubuntu-latest\n"
            "    permissions:\n"
            "      contents: write\n"
            "      actions: read\n"
            "    steps:\n"
            "    - name: Wait for the push tests\n"
            "      run: |\n"
            "        gh run list --workflow tests.yml --commit \"$SHA\" --event push\n"
            "        gh run watch \"$RUN_ID\" --exit-status\n"
            "    - name: Tag and publish\n"
            "      run: gh release create \"$TAG\" --verify-tag\n"
            "    - run: gh api -X GET \"repos/$GH_REPO/releases\" -f per_page=1\n"
            "    - run: gh api --method=GET \"repos/$GH_REPO/releases/tags/$TAG\"\n")
        check_release_writers(synthetic)
        self.assertEqual(release_writer_jobs(synthetic), [RELEASE_WRITER_JOB])

    def test_the_checker_notices_when_it_finds_no_writer_at_all(self):
        mutated = self.text
        for verb in ("create", "edit", "upload"):
            mutated = mutated.replace("gh release %s" % verb, "true ")
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)


class PackageWorkflowTests(unittest.TestCase):
    """package.yml 按清单构建便携 ZIP 并挂到 Release 上。"""

    def test_the_package_workflow_builds_and_attaches_the_zip(self):
        package = read(".github", "workflows", "package.yml")
        self.assertIn("release:\n    types: [published]", package)
        self.assertIn("workflow_call:\n    inputs:\n      tag:", package)
        self.assertIn("workflow_dispatch:\n    inputs:\n      tag:", package)
        self.assertIn("runs-on: windows-latest", package)
        # 版本从 tag 提取，仓库里没有第二份手写版本号可与它冲突。
        self.assertIn("value=${TAG#v}", package)
        self.assertIn("python release/portable_runtime.py download", package)
        self.assertIn("python release/portable_runtime.py prepare", package)
        self.assertIn("python release/release_tools.py portable", package)
        self.assertLess(package.index("portable_runtime.py download"),
                        package.index("portable_runtime.py prepare"))
        self.assertNotIn("$RUNTIME_URL", package)
        self.assertIn("dist/wb-proxy-center-v$VERSION.zip", package)
        self.assertIn("path: dist/*.zip", package)
        self.assertIn('gh release view "$TAG"', package)
        self.assertIn('gh release upload "$TAG" dist/*.zip --clobber', package)

    def test_the_release_pipeline_calls_the_package_job(self):
        jobs = workflow_jobs(read(".github", "workflows", "release.yml"))
        package = jobs["package"]
        self.assertIn("uses: ./.github/workflows/package.yml", package)
        self.assertIn("tag: ${{ needs.release.outputs.tag }}", package)

    def test_no_dot_paths_reach_the_archive(self):
        """清单是发布包的权威来源：任何以点开头的目录或文件都不许进来。"""
        for name in tools.read_manifest():
            self.assertFalse(
                any(part.startswith(".") for part in name.split("/")),
                "%s 藏在点开头的路径里" % name)

    def test_user_data_cannot_reach_the_archive(self):
        # 清单是唯一来源，更新器的受保护目录都在 .gitignore 里，
        # 发布包不可能带上维护者机器上的 accounts/ 或 usage/。
        ignored = read(".gitignore")
        for pattern in ("accounts/*.json", "usage/*.jsonl"):
            self.assertIn(pattern, ignored)


if __name__ == "__main__":
    unittest.main()
