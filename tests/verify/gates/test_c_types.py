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
"""

import json
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
