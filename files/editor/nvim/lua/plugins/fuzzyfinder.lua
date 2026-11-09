return {
  'nvim-telescope/telescope.nvim',
  dependencies = {
    'nvim-lua/plenary.nvim',
    "neovim/nvim-lspconfig",
  },
  cmd = 'Telescope',
  keys = function()
    local builtin = require('telescope.builtin')

    vim.cmd.nnoremenu([[PopUp.Goto\ Definition]], ":Telescope lsp_definitions<CR>")
    vim.cmd.nnoremenu([[PopUp.Goto\ Implementation]], ":Telescope lsp_implementations<CR>")
    vim.cmd.nnoremenu([[PopUp.Goto\ Type Definition]], ":Telescope lsp_type_definitions<CR>")
    vim.cmd.nnoremenu([[PopUp.Show\ References]], ":Telescope lsp_references<CR>")

    return {
      { '<Leader>ff', builtin.find_files, desc = 'Find files' },
      { '<Leader>fg', builtin.live_grep, desc = 'Search text' },
      { '<Leader>fb', builtin.buffers, desc = 'Find buffers' },
      { '<Leader>fs', builtin.lsp_workspace_symbols, desc = 'Find workspace symbols' },
      { '<Leader>R',  builtin.resume },
      { 'gi',         builtin.lsp_implementations,  desc = 'Goto implementations' },
      { 'gr',         builtin.lsp_references,       desc = 'Find references' },
      { 'gd',         builtin.lsp_definitions,      desc = 'Goto definitions' },
      { 'gD',         builtin.lsp_type_definitions, desc = 'Goto type definitions' },
      {
        '<C-p>',
        function()
          builtin.buffers({ previewer = false, default_selection_index = 2 })
        end
      },
    }
  end,
  opts = function()
    return {
      defaults = {
        mappings = {
          i = {
            ['<Esc>'] = require('telescope.actions').close,
            ['<C-c>'] = false,
            ['<M-p>'] = require('telescope.actions.layout').toggle_preview,
          },
        },
        layout_strategy = 'vertical',
        layout_config = {
          preview_cutoff = 10,
          height = { padding = 0 },
        },
      },
      pickers = {
        find_files = {
          follow = true,
          previewer = false,
        },
        buffers = {
          sort_mru = true,
        },
      },
    }
  end,
}
