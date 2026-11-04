//! Terminal-following colors and shared widget styles.
//!
//! The TUI draws only with the terminal's default foreground and background
//! and its 16-color ANSI palette, so it inherits the user's theme,
//! transparency, and light or dark preference instead of painting its own.

use std::fmt::Display;

use ratatui::{
    prelude::*,
    widgets::{Block, BorderType, Borders},
};

pub const ACCENT: Color = Color::Cyan;
pub const MUTED: Color = Color::DarkGray;
pub const OK: Color = Color::Green;
pub const WARN: Color = Color::Yellow;
pub const ERROR: Color = Color::Red;
pub const NOTE: Color = Color::Magenta;
pub const INFO: Color = Color::Blue;

/// Secondary text. Unlike `muted`, it stays readable on the selection bar.
pub fn faint() -> Style {
    Style::new().add_modifier(Modifier::DIM)
}

pub fn muted() -> Style {
    Style::new().fg(MUTED)
}

pub fn strong() -> Style {
    Style::new().add_modifier(Modifier::BOLD)
}

pub fn heading() -> Style {
    Style::new().fg(ACCENT).add_modifier(Modifier::BOLD)
}

/// Selected rows and options: a neutral bar that keeps each cell's own color.
pub fn selection() -> Style {
    Style::new().bg(MUTED).add_modifier(Modifier::BOLD)
}

pub fn status_color(status: &str) -> Color {
    match status {
        "ok" => OK,
        "warn" => WARN,
        "error" => ERROR,
        "info" => INFO,
        _ => MUTED,
    }
}

/// Dark text on a colored background; ANSI black follows the terminal palette.
pub fn chip_style(color: Color) -> Style {
    Style::new()
        .fg(Color::Black)
        .bg(color)
        .add_modifier(Modifier::BOLD)
}

pub fn chip(label: impl Display, color: Color) -> Span<'static> {
    Span::styled(format!(" {label} "), chip_style(color))
}

/// A key cap such as ESC, drawn like the selection bar.
pub fn key(label: impl Display) -> Span<'static> {
    Span::styled(format!(" {label} "), selection())
}

/// Rounded panel with a dim border.
pub fn panel<'a>() -> Block<'a> {
    Block::new()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(muted())
}

/// Rounded panel with an accent border, for focused inputs and dialogs.
pub fn focus_panel<'a>() -> Block<'a> {
    panel().border_style(Style::new().fg(ACCENT))
}

/// Panel title in the terminal's own foreground, so it stands out from the
/// dim border it sits on.
pub fn title<'a>(text: impl Display) -> Line<'a> {
    Line::from(Span::styled(format!(" {text} "), strong().fg(Color::Reset)))
}
