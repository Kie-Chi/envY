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
pub const MUTED: Color = Color::Reset;
pub const SUBTLE: Color = Color::DarkGray;
pub const OK: Color = Color::Green;
pub const WARN: Color = Color::Yellow;
pub const ERROR: Color = Color::Red;
pub const NOTE: Color = Color::Magenta;
pub const INFO: Color = Color::Blue;

/// Secondary text keeps the terminal foreground, including on selection bars.
/// Weight and layout provide hierarchy without reducing text contrast.
pub fn faint() -> Style {
    muted()
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
    Style::new().bg(SUBTLE).add_modifier(Modifier::BOLD)
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

/// Colored chips use ANSI black; neutral chips retain the terminal foreground.
pub fn chip_style(color: Color) -> Style {
    if color == Color::Reset {
        return selection().fg(Color::Reset);
    }
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
        .border_style(Style::new().fg(SUBTLE))
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

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::widgets::Widget;

    #[test]
    fn secondary_text_retains_default_foreground_without_dimming() {
        for style in [muted(), faint()] {
            assert_eq!(style.fg, Some(Color::Reset));
            assert_eq!(style.bg, None);
            assert!(!style.add_modifier.contains(Modifier::DIM));
        }
    }

    #[test]
    fn selection_and_decorative_borders_keep_their_original_color() {
        assert_eq!(selection().bg, Some(Color::DarkGray));
        assert_eq!(selection().fg, None);
        let area = Rect::new(0, 0, 8, 3);
        let mut buffer = Buffer::empty(area);
        panel().render(area, &mut buffer);
        assert_eq!(buffer[(0, 0)].fg, Color::DarkGray);
        assert_eq!(buffer[(0, 0)].bg, Color::Reset);
    }

    #[test]
    fn neutral_chips_do_not_draw_black_text_on_the_default_background() {
        let style = chip_style(status_color("unknown"));
        assert_eq!(style.fg, Some(Color::Reset));
        assert_eq!(style.bg, Some(Color::DarkGray));
        assert!(style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(chip_style(OK).fg, Some(Color::Black));
        assert_eq!(chip_style(OK).bg, Some(OK));
    }
}
