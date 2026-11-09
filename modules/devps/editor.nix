{ pkgs, ... }:

{
  programs.neovim = {
    enable = true;
    defaultEditor = true;
    vimAlias = true;
    viAlias = false;
    withRuby = false;
    withPython3 = false;

    extraPackages = with pkgs; [
      # LSP servers
      clang-tools          # includes clangd
      pyright
      lua-language-server
      zls
      texlab
      tinymist

      # Formatters
      black

      # Tools used by plugins (telescope, etc.)
      ripgrep
      fd
    ];
  };

  # Neovim config files managed declaratively by Home Manager.
  xdg.configFile = {
    "nvim/init.lua".source = ../../files/editor/nvim/init.lua;
    "nvim/lua/editor.lua".source = ../../files/editor/nvim/lua/editor.lua;
    "nvim/lua/util.lua".source = ../../files/editor/nvim/lua/util.lua;
    "nvim/lua/config.lua".source = ../../files/editor/nvim/lua/config.lua;
    "nvim/lua/plugins/completion.lua".source = ../../files/editor/nvim/lua/plugins/completion.lua;
    "nvim/lua/plugins/editor.lua".source = ../../files/editor/nvim/lua/plugins/editor.lua;
    "nvim/lua/plugins/fuzzyfinder.lua".source = ../../files/editor/nvim/lua/plugins/fuzzyfinder.lua;
    "nvim/lua/plugins/git.lua".source = ../../files/editor/nvim/lua/plugins/git.lua;
    "nvim/lua/plugins/lsp.lua".source = ../../files/editor/nvim/lua/plugins/lsp.lua;
    "nvim/lua/plugins/theme.lua".source = ../../files/editor/nvim/lua/plugins/theme.lua;
    "nvim/lua/plugins/treesitter.lua".source = ../../files/editor/nvim/lua/plugins/treesitter.lua;
    "nvim/lua/plugins/ui.lua".source = ../../files/editor/nvim/lua/plugins/ui.lua;
    "nvim/lua/plugins/coc.lua".source = ../../files/editor/nvim/lua/plugins/coc.lua;
  };

  # Vim config files (for server deployment / fallback via plain Vim)
  home.file = {
    ".vim/vimrc".source = ../../files/editor/vim/vimrc;
    ".vim/colors/tokyonight.vim".source = pkgs.vimPlugins.tokyonight-nvim.src + "/extras/vim/colors/tokyonight-night.vim";
    ".vim/layers/edit-essential.vim".source = ../../files/editor/vim/layers/edit-essential.vim;
    ".vim/layers/lang.vim".source = ../../files/editor/vim/layers/lang.vim;
    ".vim/layers/theme.vim".source = ../../files/editor/vim/layers/theme.vim;
    ".vim/layers/coc.vim".source = ../../files/editor/vim/layers/coc.vim;
  };
}
