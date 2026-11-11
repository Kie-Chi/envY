" The theme layer.
if index(g:rerun_layer, expand('<sfile>')) == -1
    call add(g:rerun_layer, expand('<sfile>'))
    Plug 'vim-airline/vim-airline'
else
" always show current position
let g:airline#extensions#default#section_truncate_width = {
    \ 'b': 79,
    \ 'x': 60,
    \ 'y': 88,
    \ 'warning': 80,
    \ 'error': 80,
    \ }


if has('termguicolors')
    set termguicolors
end
" let g:airline_theme = 'catppuccin'
let g:airline#extensions#csv#enabled = 1

" Keep split boundaries visible when Tokyo Night is loaded or reloaded.
set fillchars+=vert:│
augroup envy_tokyonight_separators
    autocmd!
    autocmd ColorScheme tokyonight highlight VertSplit guifg=#7aa2f7 guibg=#1a1b26 ctermfg=111 ctermbg=NONE gui=NONE cterm=NONE
augroup END

try
    colorscheme tokyonight
    " highlight CocHighlightText guibg=#505050
    " highlight clear SpellBad
    " highlight SpellBad gui=undercurl
    " highlight GitGutter ctermfg=White guifg=#a0a0a0
    " " Fix wide character issue in alacritty
    highlight Normal guibg=NONE
    highlight @text.literal.markdown_inline guibg=NONE
catch
    echomsg 'theme failed to load'
endtry
end
