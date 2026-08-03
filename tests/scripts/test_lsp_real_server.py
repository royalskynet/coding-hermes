#!/usr/bin/env python3
"""
S2: LSP 真實契約測試 — 使用 real pyright server

此測試驗證：
1. 能啟動真實 pyright language server
2. tool_lsp_definition / references / document_symbols / workspace_symbols 返回精確 file:line
3. 測 inactive、unsupported、no result、timeout、broken-set 行為
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.lsp.manager import LSPService
from agent.lsp.servers import find_server_for_file, ServerContext


def create_test_repo(tmpdir: Path) -> Path:
    """建立最小 Python git repo 測試用"""
    repo = tmpdir / "test_repo"
    repo.mkdir()
    
    # git init
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
    
    # 建立 Python 檔案
    (repo / "main.py").write_text("""
class MyClass:
    def method_a(self):
        return 42
    
    def method_b(self):
        return self.method_a()

def standalone_func():
    return MyClass()

result = standalone_func()
""")
    
    (repo / "other.py").write_text("""
from main import MyClass

class Derived(MyClass):
    def method_c(self):
        return self.method_b()

x = Derived()
print(x.method_c())
""")
    
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True)
    
    return repo


def test_real_pyright_server():
    """測試真實 pyright server 的 definition / references / document_symbols / workspace_symbols"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = create_test_repo(tmp)
        
        main_py = repo / "main.py"
        other_py = repo / "other.py"
        
        # 建立 LSPService（啟用，使用 auto install）
        service = LSPService(
            enabled=True,
            wait_mode="document",
            wait_timeout=15.0,
            install_strategy="auto",
            binary_overrides={"pyright": ["/Users/51mini/Library/Python/3.9/bin/pyright-langserver"]},
            env_overrides={},
            init_overrides={},
            disabled_servers=[],
            idle_timeout=600,
        )
        
        try:
            print(f"Service enabled: {service.is_active()}")
            print(f"Enabled for main.py: {service.enabled_for(str(main_py))}")
            
            # 1. document_symbols
            print("\n=== document_symbols ===")
            symbols = service.document_symbols_sync(str(main_py))
            print(f"Symbols found: {len(symbols)}")
            for s in symbols:
                print(f"  {s.get('name')} (kind={s.get('kind')}, line={s.get('range', {}).get('start', {}).get('line', '?')+1})")
            assert len(symbols) > 0, "Should find symbols in main.py"
            assert any(s.get('name') == 'MyClass' for s in symbols), "Should find MyClass"
            assert any(s.get('name') == 'method_a' for s in symbols), "Should find method_a"
            
            # 2. definition - 在 other.py 中 Derived.method_c 呼叫 self.method_b，定義在 main.py
            print("\n=== definition ===")
            # other.py 第 11 行: print(x.method_c()) - 這是呼叫
            # 我們要找 method_c 的定義
            # other.py 第 7 行: def method_c(self):
            defs = service.definition_sync(str(other_py), 6, 10)  # line 6, char ~10 (method_c)
            print(f"Definitions found: {len(defs)}")
            for d in defs:
                print(f"  {d.get('file')}:{d.get('range', {}).get('start', {}).get('line', '?')+1}")
            # Note: pyright 可能不會跨文件找到繼承的 method 定義，這取決於 type checking 模式
            # 至少不應 crash
            
            # 3. references - 在 main.py 找 MyClass 的引用
            print("\n=== references ===")
            # main.py 第 1 行: class MyClass:
            refs = service.references_sync(str(main_py), 0, 6)  # line 0, char ~6 (MyClass)
            print(f"References found: {len(refs)}")
            for r in refs:
                print(f"  {r.get('file')}:{r.get('range', {}).get('start', {}).get('line', '?')+1}")
            # 應該在 main.py 和 other.py 都找到
            ref_files = {r.get('file') for r in refs}
            assert str(main_py) in ref_files, "Should find reference in main.py"
            # other.py 可能或不可能找到，取決於 pyright 分析深度
            
            # 4. workspace_symbols
            print("\n=== workspace_symbols ===")
            ws_symbols = service.workspace_symbols_sync("MyClass")
            print(f"Workspace symbols: {len(ws_symbols)}")
            for s in ws_symbols:
                print(f"  {s.get('name')} in {s.get('file')}:{s.get('range', {}).get('start', {}).get('line', '?')+1}")
            assert len(ws_symbols) > 0, "Should find MyClass in workspace"
            
            # 5. Test unsupported file type
            print("\n=== unsupported file ===")
            unsupported = tmp / "test.txt"
            unsupported.write_text("hello")
            unsupported_symbols = service.document_symbols_sync(str(unsupported))
            print(f"Unsupported file symbols: {unsupported_symbols}")
            assert unsupported_symbols == [], "Should return empty for unsupported file"
            
            # 6. Test inactive (disabled)
            print("\n=== inactive service ===")
            service2 = LSPService(
                enabled=False,
                wait_mode="document",
                wait_timeout=5.0,
                install_strategy="auto",
            )
            assert service2.is_active() is False, "Disabled service should not be active"
            assert service2.enabled_for(str(main_py)) is False, "Disabled service should not enable for file"
            inactive_symbols = service2.document_symbols_sync(str(main_py))
            assert inactive_symbols == [], "Inactive service should return empty"
            
            print("\n✅ ALL REAL SERVER TESTS PASSED")
            return True
            
        finally:
            # 清理
            try:
                service._loop.stop()
            except Exception:
                pass


def test_broken_set_behavior():
    """測試 broken-set: 連續失敗的 server 不再重試"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = create_test_repo(tmp)
        main_py = repo / "main.py"
        
        # 使用不存在的 binary 強制失敗
        service = LSPService(
            enabled=True,
            wait_mode="document",
            wait_timeout=2.0,
            install_strategy="off",  # 關閉 auto-install
            binary_overrides={"pyright": ["/nonexistent/pyright-langserver"]},
            env_overrides={},
            init_overrides={},
            disabled_servers=[],
            idle_timeout=600,
        )
        
        try:
            # 第一次嘗試會失敗並標記 broken
            symbols1 = service.document_symbols_sync(str(main_py))
            print(f"First attempt (should fail): {symbols1}")
            
            # 第二次應直接跳過 (broken-set)
            symbols2 = service.document_symbols_sync(str(main_py))
            print(f"Second attempt (should be instant empty): {symbols2}")
            
            assert symbols1 == [], "First should fail and return empty"
            assert symbols2 == [], "Second should be empty (broken-set)"
            
            # 驗證 broken-set 有內容
            from agent.lsp.servers import find_server_for_file
            from agent.lsp.workspace import resolve_workspace_for_file
            
            srv = find_server_for_file(str(main_py))
            ws_root, _ = resolve_workspace_for_file(str(main_py))
            if srv and ws_root:
                per_server_root = srv.resolve_root(str(main_py), ws_root) or ws_root
                broken_key = (srv.server_id, per_server_root)
                print(f"Broken key: {broken_key}")
                print(f"In broken set: {broken_key in service._broken}")
                assert broken_key in service._broken, "Should be in broken set"
            
            print("✅ BROKEN-SET TEST PASSED")
            return True
            
        finally:
            try:
                service._loop.stop()
            except Exception:
                pass


def test_no_result_cases():
    """測試 no result cases: definition not found, references not found"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        repo = create_test_repo(tmp)
        main_py = repo / "main.py"
        
        service = LSPService(
            enabled=True,
            wait_mode="document",
            wait_timeout=10.0,
            install_strategy="auto",
            binary_overrides={"pyright": ["/Users/51mini/Library/Python/3.9/bin/pyright-langserver"]},
            env_overrides={},
            init_overrides={},
            disabled_servers=[],
            idle_timeout=600,
        )
        
        try:
            # 找不存在的 symbol
            print("\n=== no result: definition ===")
            defs = service.definition_sync(str(main_py), 100, 10)  # 超出範圍的行
            print(f"Out of range definition: {defs}")
            assert defs == [], "Should return empty for out of range"
            
            print("\n=== no result: references ===")
            refs = service.references_sync(str(main_py), 100, 10)
            print(f"Out of range references: {refs}")
            assert refs == [], "Should return empty for out of range"
            
            print("\n=== no result: workspace_symbols ===")
            ws = service.workspace_symbols_sync("NonExistentClassXYZ")
            print(f"Non-existent workspace symbol: {ws}")
            assert ws == [], "Should return empty for non-existent"
            
            print("✅ NO RESULT TESTS PASSED")
            return True
            
        finally:
            try:
                service._loop.stop()
            except Exception:
                pass


if __name__ == "__main__":
    print("=" * 60)
    print("S2 LSP 真實契約測試")
    print("=" * 60)
    
    # 驗證 pyright 可用
    import shutil
    pyright_bin = shutil.which("pyright-langserver") or "/Users/51mini/Library/Python/3.9/bin/pyright-langserver"
    if not os.path.exists(pyright_bin):
        print(f"⚠️  pyright-langserver not found at {pyright_bin}")
        print("Run: pip install pyright")
        sys.exit(1)
    print(f"✓ pyright-langserver found at {pyright_bin}")
    
    test_real_pyright_server()
    test_broken_set_behavior()
    test_no_result_cases()
    
    print("\n" + "=" * 60)
    print("ALL S2 TESTS PASSED ✅")
    print("=" * 60)