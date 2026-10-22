# SPDX-License-Identifier: MIT
"""alx.verify.gates.c_types: decision 12's closed list of declared types, over clang's AST.

No compiler runs here: the AST is written as the JSON clang dumps, and the clang call is a scripted
``subprocess.run``. The real clang is the analyze lanes' business.

Proofs (ALX-1564):
  P677 a type spelling is reduced to its names: const, volatile, struct, enum, pointers and arrays go,
       and so does the place clang spells an unnamed record with
  P678 the list: the twelve, void and the repository's own prefix pass; int, unsigned, long, size_t
       and the rest are never declared; any other name is a foreign type
  P679 every variable, parameter, field, function return and typedef of a checked file is judged,
       with the file and line clang names only when they change - after a node's own location,
       its range and, inside a macro, the spelling as well; other files are not
  P680 the named exceptions: main, a function implementing a foreign interface, a qsort comparator
  P681 the declarations come from clang's AST dump run with the file's own compile command; main()
       exits 0 on PASS and 1 on FAIL, a failing clang is an error and never a pass
  P684 a file of the database is relative to its directory, and so is a file clang names; a root
       adds every C file under it, headers among them; an excluded name leaves out a file and,
       under the root, a folder
  P697 --jobs dumps several ASTs at once, every file is dumped and every finding reported once
  P701 an array's bound is not a type, a variable-length array's variable included, and a builtin
       clang declares at its first call is no file's, its parameters with it
  P702 the repository's named exceptions: --callback names a function of a foreign interface
       under the prefix, which keeps its signature; --exempt FILE:NAME a declaration a decision keeps
  P703 --pending lists a file's foreign types without failing and still fails its other findings;
       a pending file without a foreign type fails, and so does a pending name no file carries
  P705 --backend names the files that talk to a vendor, by a folder's name or a file glob: a
       foreign type declared there is the vendor's, listed and not failing; the never-declared
       types fail there too, and a foreign type outside a backend fails as before (rule 1 as
       amended, D55)
"""

import json
import os
import subprocess

import pytest

from alx.verify.gates import c_types


def _decl(kind, name, qual, **extra):
    return {"kind": kind, "name": name, "type": {"qualType": qual}, **extra}


def _ast(file, *nodes, line=10):
    first = dict(nodes[0])
    first["loc"] = {"file": file, "line": line}
    return {"kind": "TranslationUnitDecl", "inner": [first, *nodes[1:]]}


def test_ALX1564_P677_a_type_spelling_is_reduced_to_its_names():
    assert c_types.type_names("const uint8_t *const") == ["uint8_t"]
    assert c_types.type_names("struct AlxIoPin **") == ["AlxIoPin"]
    assert c_types.type_names("volatile uint32_t [16]") == ["uint32_t"]
    assert c_types.type_names("void (*)(void *)") == ["void", "void"]
    assert c_types.type_names("unsigned long long") == ["unsigned", "long", "long"]
    assert c_types.type_names("union (unnamed at D:\\lib\\alxX.c:219:2)") == []
    assert c_types.type_names("struct (anonymous struct at alxX.h:12:5) *") == []


def test_ALX1564_P678_the_closed_list_and_what_never_gets_declared():
    for ok in (
        ["uint8_t"],
        ["int64_t"],
        ["float"],
        ["bool"],
        ["char"],
        ["void"],
        ["AlxFifo"],
        ["Alx_Status"],
    ):
        assert c_types.judge(ok, ["Alx"]) is None, ok
    assert c_types.judge(["FooMain"], ["Alx", "Foo"]) is None, "a second prefix of the repository"
    for never in (
        ["int"],
        ["unsigned", "int"],
        ["long"],
        ["size_t"],
        ["unsigned", "char"],
        ["uintptr_t"],
        ["_Bool"],
        ["long", "double"],
    ):
        what = c_types.judge(never, ["Alx"])
        assert what is not None
        assert what.startswith("never declared"), never
    assert c_types.judge(["HAL_StatusTypeDef"], ["Alx"]) == "a foreign type: HAL_StatusTypeDef"


def test_ALX1564_P679_every_declaration_of_a_checked_file_is_judged(tmp_path):
    own = str(tmp_path / "alxX.c")
    other = str(tmp_path / "vendor.h")
    ast = {
        "kind": "TranslationUnitDecl",
        "inner": [
            {**_decl("TypedefDecl", "Vendor_t", "unsigned int"), "loc": {"file": other, "line": 3}},
            {**_decl("VarDecl", "count", "int"), "loc": {"file": own, "line": 7}},
            {
                **_decl("FunctionDecl", "AlxX_Get", "long (AlxX *)"),
                "loc": {"line": 9},
                "inner": [{**_decl("ParmVarDecl", "me", "AlxX *"), "loc": {"line": 9}}],
            },
            {
                "kind": "RecordDecl",
                "name": "AlxX",
                "loc": {"line": 12},
                "inner": [
                    {**_decl("FieldDecl", "len", "size_t"), "loc": {"line": 14}},
                    {**_decl("FieldDecl", "buff", "uint8_t *"), "loc": {"line": 15}},
                ],
            },
            {
                **_decl("VarDecl", "a", "uint8_t"),
                "loc": {"line": 20},
                "range": {"begin": {"col": 1}, "end": {"line": 22}},
            },
            {**_decl("VarDecl", "b", "int"), "loc": {"col": 3}},
            {
                **_decl("VarDecl", "c", "long"),
                "loc": {
                    "spellingLoc": {"file": other, "line": 5},
                    "expansionLoc": {"file": own, "line": 30},
                },
            },
            {
                **_decl("VarDecl", "d", "short"),
                "loc": {"spellingLoc": {"file": other, "line": 6}, "expansionLoc": {"line": 31}},
            },
        ],
    }
    found = c_types.check_ast(ast, {c_types.key(own)}, ["Alx"])
    assert [(line, what.split(":")[0]) for _, line, what in found] == [
        (7, "variable count"),
        (9, "function AlxX_Get"),
        (14, "field len"),
        (22, "variable b"),
        (30, "variable c"),
    ], (
        "the vendor header's typedef is not this file's; the line is carried while clang omits the "
        "file, the range's end included; a macro is placed where it was expanded, and a spelling in "
        "another file moves the carried file with it"
    )


def test_ALX1564_P680_the_named_exceptions(tmp_path):
    own = str(tmp_path / "alxX.c")
    ast = _ast(
        own,
        _decl(
            "FunctionDecl",
            "main",
            "int (int, char **)",
            inner=[_decl("ParmVarDecl", "argc", "int"), _decl("ParmVarDecl", "argv", "char **")],
        ),
        _decl(
            "FunctionDecl",
            "HAL_UART_RxCpltCallback",
            "void (UART_HandleTypeDef *)",
            inner=[_decl("ParmVarDecl", "huart", "UART_HandleTypeDef *")],
        ),
        _decl("FunctionDecl", "AlxX_Compare", "int (const void *, const void *)"),
        _decl("FunctionDecl", "AlxX_Bad", "int (void)"),
    )
    found = c_types.check_ast(ast, {c_types.key(own)}, ["Alx"])
    assert [what for _, _, what in found] == ["function AlxX_Bad: int: never declared: int"]


def test_ALX1564_P681_clang_runs_with_the_files_own_command_and_main_reports(
    tmp_path, monkeypatch, capsys
):
    src = tmp_path / "alxX.c"
    src.write_text("int count;\n", encoding="ascii")
    db = tmp_path / "compile_commands.json"
    db.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": str(src),
                    "arguments": ["clang", "-std=gnu99", "-DX=1", "-c", str(src)],
                }
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        ast = _ast(str(src), _decl("VarDecl", "count", "int"), line=1)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(ast), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = tmp_path / "report.txt"
    assert c_types.main([str(db), "--clang", "clang-x", "--out", str(out)]) == 1
    argv = calls[0]
    assert argv[0] == "clang-x"
    assert "-DX=1" in argv, "the file's own command"
    assert "-c" not in argv
    assert argv[-4:] == ["-fsyntax-only", "-Xclang", "-ast-dump=json", str(src)]
    printed = capsys.readouterr().out
    assert printed.startswith("C TYPES GATE: FAIL")
    assert out.read_text(encoding="ascii") == printed

    clean = _ast(str(src), _decl("VarDecl", "count", "uint32_t"), line=1)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(clean), stderr=""
        ),
    )
    assert c_types.main([str(db)]) == 0
    assert capsys.readouterr().out == "C TYPES GATE: PASS (1 files)\n"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="error: no such file"
        ),
    )
    with pytest.raises(RuntimeError, match="clang failed"):
        c_types.main([str(db)])


def test_ALX1564_P684_a_root_adds_every_c_file_and_exclude_leaves_out_a_file_or_folder(tmp_path):
    lib = tmp_path / "lib"
    for name in (
        "alxX.c",
        "alxX.h",
        "Ext/alxY.h",
        "Ext/lfs/lfs.c",
        "Test/fakes/fooFake.c",
        "a.txt",
    ):
        (lib / name).parent.mkdir(parents=True, exist_ok=True)
        (lib / name).write_text("", encoding="ascii")
    app = tmp_path / "app.c"
    entries = [
        {"directory": str(lib), "file": "alxX.c"},
        {"directory": str(lib), "file": str(lib / "Ext" / "lfs" / "lfs.c")},
        {"directory": str(tmp_path), "file": str(app)},
    ]

    def keys(*names):
        return {c_types.key(tmp_path / n) for n in names}

    assert c_types.checked_files(entries, None, []) == keys(
        "lib/alxX.c", "lib/Ext/lfs/lfs.c", "app.c"
    )
    assert c_types.checked_files(entries, None, ["lfs.c"]) == keys("lib/alxX.c", "app.c")
    assert c_types.checked_files(entries, str(lib), ["lfs", "Test"]) == keys(
        "lib/alxX.c", "lib/alxX.h", "lib/Ext/alxY.h", "app.c"
    ), "every C file under the root, the excluded folders out of the database's files too"

    ast = _ast("alxX.h", _decl("VarDecl", "count", "int"))
    checked = keys("lib/alxX.h")
    found = c_types.check_ast(ast, checked, ["Alx"], lib)
    assert [what for _, _, what in found] == ["variable count: int: never declared: int"]
    assert c_types.check_ast(ast, checked, ["Alx"]) == [], "relative to where clang ran, not to us"


def test_ALX1564_P697_jobs_dump_several_asts_at_once_and_report_each_finding_once(
    tmp_path, monkeypatch, capsys
):
    sources = [tmp_path / "alxA.c", tmp_path / "alxB.c", tmp_path / "alxC.c"]
    for src in sources:
        src.write_text("int count;\n", encoding="ascii")
    db = tmp_path / "compile_commands.json"
    entries = [
        {"directory": str(tmp_path), "file": str(s), "arguments": ["clang", str(s)]}
        for s in sources
    ]
    db.write_text(json.dumps(entries), encoding="utf-8")
    header = str(tmp_path / "alxShared.h")
    (tmp_path / "alxShared.h").write_text("long shared;\n", encoding="ascii")  # the root adds it
    dumped = []

    def fake_run(argv, **kw):
        dumped.append(argv[-1])
        own = _ast(argv[-1], _decl("VarDecl", "count", "int"), line=1)
        shared = _ast(header, _decl("VarDecl", "shared", "long"), line=3)  # every file includes it
        ast = {"kind": "TranslationUnitDecl", "inner": [*own["inner"], *shared["inner"]]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(ast), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert c_types.main([str(db), "--jobs", "3", "--root", str(tmp_path)]) == 1
    assert sorted(dumped) == sorted(str(s) for s in sources)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "C TYPES GATE: FAIL (3 files)"
    expected = [f"{s}:1: variable count: int: never declared: int" for s in sources]
    expected.append(f"{header}:3: variable shared: long: never declared: long")
    assert lines[1:] == sorted(expected), "the shared header's finding once, whoever reached it"


def test_ALX1564_P701_an_array_bound_is_not_a_type_and_a_builtin_is_no_files(tmp_path):
    assert c_types.type_names("uint8_t[regLen]") == ["uint8_t"], "a variable-length array"
    assert c_types.type_names("const uint8_t *[ALX_LEN + 1]") == ["uint8_t"]
    own = str(tmp_path / "alxX.c")
    builtin = {
        **_decl("FunctionDecl", "__builtin_va_start", "void (__builtin_va_list &, ...)"),
        "isImplicit": True,
        "inner": [_decl("ParmVarDecl", "", "__builtin_va_list &")],
    }
    ast = _ast(
        own, _decl("VarDecl", "data", "uint8_t[regLen]"), builtin, _decl("VarDecl", "n", "int")
    )
    found = c_types.check_ast(ast, {c_types.key(own)}, ["Alx"])
    assert [what for _, _, what in found] == ["variable n: int: never declared: int"], (
        "the bound is the array's, and the builtin's parameter is clang's own"
    )


def test_ALX1564_P702_callbacks_and_exempt_declarations_are_the_repositorys_named_exceptions(
    tmp_path,
):
    own = str(tmp_path / "alxFs.c")
    callback = _decl(
        "FunctionDecl",
        "AlxFs_Lfs_Read",
        "int (const struct lfs_config *, lfs_block_t)",
        inner=[
            _decl("ParmVarDecl", "c", "const struct lfs_config *"),
            _decl("ParmVarDecl", "block", "lfs_block_t"),
        ],
    )
    frozen = _decl("VarDecl", "consumed", "int")
    other = _decl("VarDecl", "count", "int")
    ast = _ast(own, callback, frozen, other)
    checked = {c_types.key(own)}
    assert len(c_types.check_ast(ast, checked, ["Alx"])) == 5, "under the prefix it is judged"
    found = c_types.check_ast(
        ast, checked, ["Alx"], callbacks=["AlxFs_Lfs_Read"], exempt=["alxFs.c:consumed"]
    )
    assert [what for _, _, what in found] == ["variable count: int: never declared: int"], (
        "the callback keeps its signature and the exempt declaration its type; the file's name and "
        "the declared name together select it"
    )
    assert len(c_types.check_ast(ast, checked, ["Alx"], exempt=["alxOther.c:consumed"])) == 5


def test_ALX1564_P703_pending_lists_a_files_foreign_types_and_fails_the_rest(
    tmp_path, monkeypatch, capsys
):
    sources = {name: tmp_path / name for name in ("alxA.c", "alxB.c", "alxC.c")}
    for src in sources.values():
        src.write_text("", encoding="ascii")
    db = tmp_path / "compile_commands.json"
    entries = [
        {"directory": str(tmp_path), "file": str(s), "arguments": ["clang", str(s)]}
        for s in sources.values()
    ]
    db.write_text(json.dumps(entries), encoding="utf-8")
    declared = {
        "alxA.c": [_decl("FieldDecl", "hi2c", "I2C_HandleTypeDef")],
        "alxB.c": [_decl("VarDecl", "status", "HAL_StatusTypeDef"), _decl("VarDecl", "i", "int")],
        "alxC.c": [_decl("VarDecl", "ok", "uint8_t")],
    }

    def fake_run(argv, **kw):
        ast = _ast(argv[-1], *declared[argv[-1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]], line=4)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(ast), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert c_types.main([str(db), "--pending", "alxA.c"]) == 1, "alxB.c's are not pending"
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "C TYPES GATE: FAIL (3 files)"
    assert lines[-2] == "Pending: 1 foreign types in 1 files wait for a decision"
    assert lines[-1].endswith(
        "alxA.c:4: field hi2c: I2C_HandleTypeDef: a foreign type: I2C_HandleTypeDef"
    )

    assert c_types.main([str(db), "--pending", "alxA.c", "--pending", "alxB.c"]) == 1
    failing = capsys.readouterr().out.split("Pending:")[0]
    assert "variable i: int: never declared: int" in failing, "a pending file's int still fails"
    assert "HAL_StatusTypeDef" not in failing

    declared["alxB.c"] = [_decl("VarDecl", "status", "HAL_StatusTypeDef")]
    assert c_types.main([str(db), "--pending", "alxA.c", "--pending", "alxB.c"]) == 0
    assert capsys.readouterr().out.startswith("C TYPES GATE: PASS (3 files)\nPending: 2 foreign")

    assert c_types.main([str(db), "--pending", "alxC.c", "--pending", "alxGone.c"]) == 1
    out = capsys.readouterr().out
    clean, gone = (os.path.normcase(n) for n in ("alxC.c", "alxGone.c"))
    assert f"{clean}: pending, and declares no foreign type - take it off the list" in out
    assert f"{gone}: pending, and not checked" in out


def test_ALX1564_P705_a_backend_declares_its_vendors_types_and_nothing_else_does(
    tmp_path, monkeypatch, capsys
):
    lib = tmp_path / "lib"
    names = ("alxFtp.c", "alxOsThread.c", "Mcu/McuStm32/alxI2c_McuStm32.c", "Mcu/alxTrace.c")
    for name in names:
        (lib / name).parent.mkdir(parents=True, exist_ok=True)
        (lib / name).write_text("", encoding="ascii")
    db = tmp_path / "compile_commands.json"
    entries = [
        {"directory": str(lib), "file": name, "arguments": ["clang", name]} for name in names
    ]
    db.write_text(json.dumps(entries), encoding="utf-8")
    declared = {
        "alxFtp.c": [_decl("VarDecl", "len", "size_t"), _decl("VarDecl", "t", "TickType_t")],
        "alxOsThread.c": [_decl("FieldDecl", "handle", "TaskHandle_t")],
        "alxI2c_McuStm32.c": [
            _decl("FieldDecl", "hi2c", "I2C_HandleTypeDef"),
            _decl("VarDecl", "status", "HAL_StatusTypeDef"),
            _decl("VarDecl", "i", "int"),
        ],
        "alxTrace.c": [_decl("ParmVarDecl", "args", "va_list")],
    }

    def fake_run(argv, **kw):
        name = argv[-1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        ast = _ast(argv[-1], *declared[name], line=7)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(ast), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backends = ["--backend", "Mcu", "--backend", "alxOs*.[ch]"]
    assert c_types.main([str(db), "--root", str(lib), *backends]) == 1
    out = capsys.readouterr().out
    failing, listed = out.split("Backends: ")
    assert failing.startswith("C TYPES GATE: FAIL (4 files)")
    assert "alxFtp.c:7: variable len: size_t: never declared: size_t" in failing
    assert "alxFtp.c:7: variable t: TickType_t: a foreign type: TickType_t" in failing, (
        "an RTOS type outside a backend"
    )
    assert "alxI2c_McuStm32.c:7: variable i: int: never declared: int" in failing, (
        "a backend declares no int either"
    )
    assert "HAL_StatusTypeDef" not in failing
    assert "TaskHandle_t" not in failing
    assert listed.startswith("4 vendor types in 3 files (rule 1)\n")
    assert [line.rsplit(": ", 3)[1:] for line in listed.splitlines()[1:]] == [
        ["field hi2c", "I2C_HandleTypeDef", c_types.VENDOR],
        ["variable status", "HAL_StatusTypeDef", c_types.VENDOR],
        ["parameter args", "va_list", c_types.VENDOR],
        ["field handle", "TaskHandle_t", c_types.VENDOR],
    ], "the folder's files, a file under a folder of it, and the glob's; by path"

    declared["alxFtp.c"] = [_decl("VarDecl", "ok", "bool")]
    declared["alxI2c_McuStm32.c"] = [_decl("FieldDecl", "hi2c", "I2C_HandleTypeDef")]
    assert c_types.main([str(db), "--root", str(lib), *backends]) == 0
    assert capsys.readouterr().out.startswith("C TYPES GATE: PASS (4 files)\nBackends: 3 vendor")

    assert c_types.main([str(db), "--root", str(lib)]) == 1, "without the option every one fails"
    assert "Backends:" not in capsys.readouterr().out

    keys = c_types.backend_files(entries, str(lib), ["mcu", "ALXOS*"])
    assert keys == set(), "a pattern is matched in the name's own case"
