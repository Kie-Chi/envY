import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))


@unittest.skipUnless(shutil.which("nvim"), "Neovim is required for terminal window checks")
class NeovimTerminalTests(unittest.TestCase):
    def test_terminal_gutter_stays_hidden_across_reload_and_window_changes(self):
        source = ROOT / "files/editor/nvim/lua/editor.lua"
        script = r'''
local function reload() dofile(vim.env.ENVY_EDITOR_SOURCE) end
local function terminal_window(win)
  assert(not vim.wo[win].number, 'terminal number must be hidden')
  assert(not vim.wo[win].relativenumber, 'terminal relative numbers must be hidden')
  assert(vim.wo[win].signcolumn == 'no', 'terminal signcolumn must be hidden')
  vim.cmd.redraw()
  assert(vim.fn.getwininfo(win)[1].textoff == 0, 'terminal gutter must have zero width')
end
local function editing_window(win)
  assert(vim.wo[win].number, 'editing number must be visible')
  assert(vim.wo[win].signcolumn == 'yes', 'editing signcolumn must be restored')
end
reload()
vim.opt.hidden = true
local edit = vim.api.nvim_get_current_buf()
local edit_win = vim.api.nvim_get_current_win()
vim.api.nvim_buf_set_name(edit, 'navigation-fixture.txt')
vim.api.nvim_buf_set_lines(edit, 0, -1, false, {'ordinary text'})
editing_window(edit_win)
vim.cmd('new')
vim.fn.jobstart({vim.env.ENVY_TEST_SHELL, '-c', 'cat'}, {term = true})
local term = vim.api.nvim_get_current_buf()
local term_win = vim.api.nvim_get_current_win()
terminal_window(term_win)
vim.wo.number = true
vim.wo.relativenumber = true
vim.wo.signcolumn = 'yes'
reload()
terminal_window(term_win)
vim.cmd('vsplit')
local split_win = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_buf(split_win, edit)
editing_window(split_win)
vim.api.nvim_win_set_buf(split_win, term)
terminal_window(split_win)
vim.wo.number = true
vim.wo.relativenumber = true
vim.wo.signcolumn = 'yes'
vim.api.nvim_set_current_win(edit_win)
vim.api.nvim_set_current_win(split_win)
terminal_window(split_win)
vim.api.nvim_set_current_win(edit_win)
editing_window(edit_win)
vim.cmd('tabnew')
vim.fn.jobstart({vim.env.ENVY_TEST_SHELL, '-c', 'cat'}, {term = true})
local other_term_win = vim.api.nvim_get_current_win()
vim.wo.number = true
vim.wo.relativenumber = true
vim.wo.signcolumn = 'yes'
vim.cmd('tabprevious')
vim.api.nvim_set_current_win(edit_win)
reload()
editing_window(edit_win)
terminal_window(term_win)
terminal_window(other_term_win)
vim.cmd('new')
local special_win = vim.api.nvim_get_current_win()
vim.bo.buftype = 'nofile'
vim.wo.number = false
vim.wo.signcolumn = 'auto'
vim.api.nvim_set_current_win(edit_win)
vim.api.nvim_set_current_win(special_win)
assert(not vim.wo.number and vim.wo.signcolumn == 'auto', 'special window options changed')
print('terminal window lifecycle checks passed')
vim.cmd('qa!')
'''
        with tempfile.TemporaryDirectory(prefix="envy-neovim-terminal-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            check = root / "check.lua"
            check.write_text(script)
            env = {
                "HOME": str(home),
                "USER": root.name,
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TERM": "xterm-256color",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "ENVY_EDITOR_SOURCE": str(source),
                "ENVY_TEST_SHELL": shutil.which("sh"),
            }
            try:
                result = subprocess.run(
                    [shutil.which("nvim"), "--headless", "-u", "NONE", "-i", "NONE", "-l", str(check)],
                    cwd=root, env=env, capture_output=True, text=True, timeout=15,
                )
            finally:
                shutil.rmtree(Path("/tmp") / f"vimbackup-{root.name}", ignore_errors=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("terminal window lifecycle checks passed", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
