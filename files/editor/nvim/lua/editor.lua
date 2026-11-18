vim.filetype.add({
  extension = {
    typ = 'typst',
    mpp = 'cpp',
    plt = 'gnuplot',
    gnu = 'gnuplot',
    repos = 'yaml',
  }
})

vim.treesitter.language.register('bash', 'PKGBUILD')

-- Options
vim.opt.shiftwidth = 2
vim.opt.expandtab = true
vim.opt.smarttab = true
vim.opt.number = true
vim.opt.autowrite = true
vim.opt.undofile = true
vim.opt.backup = true
vim.opt.formatoptions:append('mB')
vim.opt.selectmode:remove('mouse')
vim.opt.showmode = false
vim.opt.mouse = 'a'
vim.opt.updatetime = 300
vim.opt.ignorecase = true
vim.opt.smartcase = true
vim.opt.completeopt = "menu,menuone,noselect"
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.clipboard = 'unnamedplus'

-- OSC 52 clipboard for terminals that support it
local term = vim.env.TERM or ''
if term == 'wezterm' or term == 'xterm-kitty' or vim.env.TMUX ~= nil then
  local osc52 = require('vim.ui.clipboard.osc52')
  vim.g.clipboard = {
    name = 'OSC 52',
    copy = {
      ['+'] = osc52.copy('+'),
      ['*'] = osc52.copy('*'),
    },
    paste = {
      ['+'] = osc52.paste('+'),
      ['*'] = osc52.paste('*'),
    },
  }
end

-- create a directory to hold backup files
local backup_dir = '/tmp/vimbackup-' .. os.getenv('USER')
if vim.fn.isdirectory(backup_dir) == 0 then
  print('Creating ' .. backup_dir)
  vim.fn.mkdir(backup_dir, '', tonumber('700', 8))
end

local undo_dir = vim.fn.stdpath('state') .. '/undo'
if vim.fn.isdirectory(undo_dir) == 0 then
  print('Creating ' .. undo_dir)
  vim.fn.mkdir(undo_dir)
end

-- Backup and undofile directory:
vim.opt.undodir:append('.')
vim.opt.backupdir:prepend(backup_dir)

-- Keymaps
vim.g.mapleader = ' '

vim.keymap.set('n', '<Leader>n', '<Cmd>noh<CR>')
vim.keymap.set('n', '<Leader><Leader>', '<Cmd>ccl<CR>')
vim.keymap.set('n', '<Leader>b', '<Cmd>buf#<CR>')

vim.keymap.set({'n', 'v'}, 'j', 'gj')
vim.keymap.set({'n', 'v'}, 'k', 'gk')

-- Search centering (keeps match at screen center)
vim.keymap.set('n', 'n', 'nzz')
vim.keymap.set('n', 'N', 'Nzz')
vim.keymap.set('n', '*', '*zz')
vim.keymap.set('n', '#', '#zz')
vim.keymap.set('n', 'g*', 'g*zz')

-- Visual block reselect after indent
vim.keymap.set('v', '<', '<gv')
vim.keymap.set('v', '>', '>gv')

-- Quick save/close
vim.keymap.set('n', '<Leader>w', '<Cmd>w<CR>')
vim.keymap.set('n', '<Leader>Q', '<Cmd>q!<CR>', { desc = 'Force quit window' })
vim.keymap.set('n', '<Leader>sr', '<Cmd>source $MYVIMRC<CR>', { desc = 'Reload config' })

-- Y behaves like C/D (yank to end of line)
vim.keymap.set('n', 'Y', 'y$')

-- ; as : shortcut for command mode
vim.keymap.set('n', ';', ':')

-- H/L as line start/end (more useful than screen top/bottom)
vim.keymap.set('n', 'H', '^')
vim.keymap.set('n', 'L', '$')

-- Select all
vim.keymap.set('n', '<Leader>sa', 'ggVG')

-- kj/jj as Esc alternatives in insert mode
vim.keymap.set('i', 'kj', '<Esc>')
vim.keymap.set('i', 'jj', '<Esc>')

-- Swap ' and ` for more intuitive mark jumping
vim.keymap.set('n', "'", "`")
vim.keymap.set('n', "`", "'")

-- Command mode navigation
vim.keymap.set('c', '<C-a>', '<Home>')
vim.keymap.set('c', '<C-e>', '<End>')
vim.keymap.set('c', '<C-P>', '<Up>')
vim.keymap.set('c', '<C-N>', '<Down>')

-- Sudo save
vim.keymap.set('c', 'w!!', 'w !sudo tee >/dev/null %')

-- terminal
-- Preserve macOS line navigation when forwarding keys to the shell.
vim.keymap.set('t', '<D-Left>', '<C-a>', { desc = 'Terminal line start' })
vim.keymap.set('t', '<D-Right>', '<C-e>', { desc = 'Terminal line end' })
vim.keymap.set('t', '<C-g>', '<C-\\><C-N>', {
  desc = 'Exit terminal mode',
})
vim.keymap.set('t', '<Esc><Esc>', '<C-\\><C-N>', {
  desc = 'Exit terminal mode',
})

vim.keymap.set('n', '<Leader>tt', ':terminal<CR>')
vim.keymap.set('n', '<Leader>tv', ':vsplit | terminal<CR>')
vim.keymap.set('n', '<Leader>th', ':split | terminal<CR>')

-- Window switching (Leader + h/j/k/l)
vim.keymap.set('n', '<Leader>h', '<C-w>h', { desc = 'Window left' })
vim.keymap.set('n', '<Leader>j', '<C-w>j', { desc = 'Window down' })
vim.keymap.set('n', '<Leader>k', '<C-w>k', { desc = 'Window up' })
vim.keymap.set('n', '<Leader>l', '<C-w>l', { desc = 'Window right' })

-- Window moving (Leader + H/J/K/L)
vim.keymap.set('n', '<Leader>H', '<C-w>H', { desc = 'Move window left' })
vim.keymap.set('n', '<Leader>J', '<C-w>J', { desc = 'Move window down' })
vim.keymap.set('n', '<Leader>K', '<C-w>K', { desc = 'Move window up' })
vim.keymap.set('n', '<Leader>L', '<C-w>L', { desc = 'Move window right' })
local terminal = vim.api.nvim_create_augroup('terminal', {})
local function update_line_numbers()
  if vim.bo.buftype == 'terminal' then
    vim.wo.number = false
    vim.wo.relativenumber = false
    vim.wo.signcolumn = 'no'
  elseif vim.bo.buftype == '' then
    vim.wo.number = true
    vim.wo.signcolumn = 'yes'
  end
end

vim.api.nvim_create_autocmd({ 'TermOpen', 'BufWinEnter', 'WinEnter' }, {
  group = terminal,
  callback = update_line_numbers,
})

vim.api.nvim_create_autocmd('TermOpen', {
  pattern = '*',
  group = terminal,
  callback = function ()
    -- Restore normal j/k in terminal buffer (gj/gk doesn't work well in terminal)
    vim.keymap.set('n', 'j', 'j', { buffer = true })
    vim.keymap.set('n', 'k', 'k', { buffer = true })
    vim.keymap.set('n', '<C-P>', 'i<C-P><C-\\><C-N>', { buffer = true })
    vim.keymap.set('n', '<C-N>', 'i<C-N><C-\\><C-N>', { buffer = true })
    vim.keymap.set('n', '<CR>',  'i<CR><C-\\><C-N>G', { buffer = true })
  end
})

-- Refresh existing windows when the configuration is reloaded.
for _, win in ipairs(vim.api.nvim_list_wins()) do
  vim.api.nvim_win_call(win, update_line_numbers)
end

-- restore last edit position
vim.api.nvim_create_autocmd('BufReadPost', {
  pattern = '*',
  group = vim.api.nvim_create_augroup('session', {}),
  callback = function()
    local last_pos = vim.api.nvim_buf_get_mark(0, '"')
    local line, col = last_pos[1], last_pos[2]
    local line_count = vim.api.nvim_buf_line_count(0)
    if line > 0 and line <= line_count then
      pcall(vim.api.nvim_win_set_cursor, 0, {line, col})
    end
  end,
})

-- My shortcut commands
vim.api.nvim_create_user_command('EditInit', 'edit ' .. vim.fn.stdpath('config') .. '/init.lua', {})
vim.keymap.set('n', '<F10>', '<Cmd>!g++ % -o %<.o<CR>')

-- Strip trailing whitespace on save for common filetypes
vim.api.nvim_create_autocmd('FileType', {
  pattern = { 'c', 'cpp', 'java', 'go', 'php', 'javascript', 'puppet', 'python', 'rust', 'xml', 'yml', 'perl', 'sh', 'lua', 'vim' },
  group = vim.api.nvim_create_augroup('strip_whitespace', {}),
  callback = function()
    vim.api.nvim_create_autocmd('BufWritePre', {
      buffer = 0,
      callback = function()
        local pos = vim.fn.getpos('.')
        vim.cmd([[%s/\s\+$//e]])
        vim.fn.setpos('.', pos)
      end,
    })
  end,
})
