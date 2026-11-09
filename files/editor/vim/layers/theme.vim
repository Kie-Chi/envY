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

" ignore if onedark theme is not installed
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
