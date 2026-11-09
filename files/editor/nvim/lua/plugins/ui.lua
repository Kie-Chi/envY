return {
  {
    "folke/which-key.nvim",
    commit = "3aab2147e74890957785941f0c1ad87d0a44c15a",
    event = "VeryLazy",
    opts = {
      preset = "modern",
      delay = 300,
      spec = {
        { "<leader>f", group = "Find" },
        { "<leader>t", group = "Terminal" },
        { "<leader>c", group = "Code" },
        { "<leader>u", group = "UI" },
      },
    },
  },

  {
    "nvim-neo-tree/neo-tree.nvim",
    branch = "v3.x",
    dependencies = {
      "nvim-lua/plenary.nvim",
      "nvim-tree/nvim-web-devicons", -- not strictly required, but recommended
      "MunifTanjim/nui.nvim",
    },
    cmd = "Neotree",
    keys = {
      { "<leader>e", "<cmd>Neotree reveal<CR>" },
      { "<leader>s", "<cmd>Neotree show left toggle reveal document_symbols<cr>" }
    },
    opts = {
      event_handlers = {
        {
          event = "file_opened",
          handler = function()
            require("neo-tree.command").execute({ action = "close" })
          end
        },
      },
      window = {
        mappings = {
          ['e'] = function() vim.cmd('Neotree focus filesystem left') end,
          ['g'] = function() vim.cmd('Neotree focus git_status left') end,
          ['s'] = function() vim.cmd('Neotree focus document_symbols left') end,
          ['<tab>'] = 'toggle_node',
        },
      },
      sources = { "filesystem", "git_status", "document_symbols" },
    },
    init = function()
      vim.api.nvim_create_user_command('EditInit', function()
        require('neo-tree.command').execute({
          reveal_file = vim.fn.stdpath('config') .. '/init.lua',
          reveal_force_cwd = true,
        })
      end, { desc = 'Edit init.lua' })
    end
  },

  {
    'nvim-pack/nvim-spectre',
    dependencies = {
      'nvim-lua/plenary.nvim',
    },
    build = false,
    cmd = "Spectre",
    opts = { open_cmd = "noswapfile vnew" },
  },

  {
    'nvim-lualine/lualine.nvim',
    dependencies = {
      'nvim-tree/nvim-web-devicons',
      'arkav/lualine-lsp-progress',
      'AndreM222/copilot-lualine',
    },
    event = 'VeryLazy',
    opts = {
      extensions = { 'lazy', 'neo-tree' },
      options = {
        component_separators = '',
        section_separators = '',
      },
      sections = {
        -- lualine_c = { 'filename', '%{coc#status()}' },
        lualine_c = { 'filename', 'lsp_progress' },
        lualine_x = { 'copilot', 'encoding', 'fileformat', 'filetype' },
      },
    }
  },

  -- For vim.ui.select and vim.ui.input
  {
    'stevearc/dressing.nvim',
    opts = {},
  },

  -- For vim.notify
  {
    'rcarriga/nvim-notify',
    keys = {
      {
        "<leader>un",
        function()
          require("notify").dismiss({ silent = true, pending = true })
        end,
        desc = "Dismiss All Notifications",
      },
    },
    opts = {
      stages = 'static',
      max_width = function()
        return math.floor(vim.o.columns * 0.75)
      end,
      top_down = true,
    },
    init = function()
      local banned_messages = {
        "No matching notification found to replace",
      }
      vim.api.nvim_create_autocmd('User', {
        pattern = 'LazyDone',
        callback = function()
          vim.notify = function(msg, ...)
            if msg == nil then
              return
            end
            for _, banned in ipairs(banned_messages) do
              if string.match(msg, banned) then
                return
              end
            end
            require('notify')(msg, ...)
          end
        end
      })
    end
  },

  {
    'akinsho/toggleterm.nvim',
    version = "*",
    opts = {
      open_mapping = '<C-t>',
      direction = 'tab',
    }
  },
}
