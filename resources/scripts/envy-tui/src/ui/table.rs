//! Shared table, scrollbar, input, and empty-state widgets.

use ratatui::{
    layout::Flex,
    prelude::*,
    widgets::{
        Cell, Paragraph, Row, Scrollbar, ScrollbarOrientation, ScrollbarState, Table, TableState,
        Wrap,
    },
};

use crate::theme::{self, ACCENT};

use super::row_offset;

const HIGHLIGHT_SYMBOL: &str = "▸ ";

fn selected_state(selected: usize, offset: usize, empty: bool) -> TableState {
    TableState::default().with_selected((!empty).then(|| selected.saturating_sub(offset)))
}

/// A table column: header label, width constraint, and the narrowest table
/// that still shows it.
pub(super) struct Column {
    header: &'static str,
    width: Constraint,
    min_table_width: u16,
}

pub(super) const fn column(header: &'static str, width: Constraint) -> Column {
    optional(header, width, 0)
}

/// A secondary column, hidden when the table is narrower than `min_table_width`.
pub(super) const fn optional(
    header: &'static str,
    width: Constraint,
    min_table_width: u16,
) -> Column {
    Column {
        header,
        width,
        min_table_width,
    }
}

/// A column just wide enough for its header and widest value plus a gap,
/// up to `cap`.
pub(super) fn fitted<'a>(
    header: &'static str,
    values: impl Iterator<Item = &'a str>,
    cap: u16,
    min_table_width: u16,
) -> Column {
    let widest = values
        .map(|value| Span::raw(value).width())
        .chain([header.len()])
        .max()
        .unwrap_or(0);
    optional(
        header,
        Constraint::Length((widest + 2).min(usize::from(cap)) as u16),
        min_table_width,
    )
}

/// Styled parts of one table cell. When the cell is too narrow, later parts
/// are cut first, so secondary notes give way to the main text.
pub(super) struct TableCell {
    parts: Vec<(String, Style)>,
}

impl TableCell {
    pub(super) fn new(text: impl Into<String>) -> Self {
        Self::styled(text, Style::new())
    }

    pub(super) fn styled(text: impl Into<String>, style: Style) -> Self {
        Self {
            parts: vec![(text.into(), style)],
        }
    }

    /// Append dimmed secondary text, such as an age after a time.
    pub(super) fn note(mut self, text: impl Into<String>) -> Self {
        self.parts.push((text.into(), theme::faint()));
        self
    }

    fn fit(self, width: u16) -> Line<'static> {
        let mut room = width;
        let mut spans = Vec::new();
        for (text, style) in self.parts {
            let fits = Span::raw(text.as_str()).width() <= usize::from(room);
            let text = ellipsize(&text, room);
            room = room.saturating_sub(Span::raw(text.as_str()).width() as u16);
            spans.push(Span::styled(text, style));
            if !fits {
                break;
            }
        }
        Line::from(spans)
    }
}

/// Content of a bordered, scrollable table page.
pub(super) struct DataTable<'a> {
    pub(super) title: String,
    /// One-based selected row and row count, shown at the top right.
    pub(super) position: Option<(usize, usize)>,
    pub(super) columns: &'a [Column],
    pub(super) rows: Vec<Vec<TableCell>>,
    pub(super) selected: usize,
    pub(super) scroll: usize,
}

/// Width of each rendered column. This mirrors ratatui's own table layout so
/// cells can end in a visible ellipsis instead of being clipped silently.
fn column_widths(columns: &[&Column], table_width: u16, selection_width: u16) -> Vec<u16> {
    let [_, cells] = Layout::horizontal([Constraint::Length(selection_width), Constraint::Fill(0)])
        .areas(Rect::new(0, 0, table_width, 1));
    Layout::horizontal(columns.iter().map(|column| column.width))
        .flex(Flex::Start)
        .spacing(1)
        .split(cells)
        .iter()
        .map(|cell| cell.width)
        .collect()
}

/// Fit a value into the given number of terminal cells on one line, marking a
/// cut with an ellipsis.
pub(super) fn ellipsize(value: &str, width: u16) -> String {
    let value = value.replace(['\r', '\n'], " ");
    let width = usize::from(width);
    if Span::raw(value.as_str()).width() <= width {
        return value;
    }
    let mut result = String::new();
    let mut used = 0;
    for character in value.chars() {
        let mut buffer = [0; 4];
        let character_width = Span::raw(&*character.encode_utf8(&mut buffer)).width();
        if used + character_width >= width {
            break;
        }
        used += character_width;
        result.push(character);
    }
    if width > 0 {
        result.push('…');
    }
    result
}

pub(super) fn render_table(frame: &mut Frame, area: Rect, table: DataTable) {
    let count = table.rows.len();
    let offset = row_offset(table.scroll, count, area);
    let selection_width = if count == 0 {
        0
    } else {
        Span::raw(HIGHLIGHT_SYMBOL).width() as u16
    };
    let shown = table
        .columns
        .iter()
        .map(|column| area.width >= column.min_table_width)
        .collect::<Vec<_>>();
    let columns = table
        .columns
        .iter()
        .zip(&shown)
        .filter_map(|(column, shown)| shown.then_some(column))
        .collect::<Vec<_>>();
    let widths = column_widths(&columns, area.width.saturating_sub(2), selection_width);
    let rows = table
        .rows
        .into_iter()
        .skip(offset)
        .map(|cells| {
            Row::new(
                cells
                    .into_iter()
                    .zip(&shown)
                    .filter_map(|(cell, shown)| shown.then_some(cell))
                    .zip(&widths)
                    .map(|(cell, width)| Cell::from(cell.fit(*width)))
                    .collect::<Vec<_>>(),
            )
        })
        .collect::<Vec<_>>();
    let mut block = theme::panel().title(theme::title(&table.title));
    if let Some((selected, total)) = table.position {
        block = block.title_top(
            Line::styled(format!(" {selected} / {total} "), theme::muted()).right_aligned(),
        );
    }
    let widget = Table::new(rows, columns.iter().map(|column| column.width))
        .header(Row::new(columns.iter().map(|column| column.header)).style(theme::heading()))
        .row_highlight_style(theme::selection())
        .highlight_symbol(Span::styled(HIGHLIGHT_SYMBOL, Style::new().fg(ACCENT)))
        .block(block);
    let mut state = selected_state(table.selected, offset, count == 0);
    frame.render_stateful_widget(widget, area, &mut state);
    render_scrollbar(frame, area, count, offset);
}

/// Draw a scrollbar on the table's right border when its rows overflow.
fn render_scrollbar(frame: &mut Frame, area: Rect, count: usize, offset: usize) {
    // Skip the top border and header row above the rows and the bottom border.
    let track = Rect {
        y: area.y.saturating_add(2),
        height: area.height.saturating_sub(3),
        ..area
    };
    let visible = usize::from(track.height);
    if visible == 0 || count <= visible {
        return;
    }
    let mut state = ScrollbarState::new(count - visible + 1)
        .position(offset)
        .viewport_content_length(visible);
    frame.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some("│"))
            .track_style(theme::muted())
            .thumb_symbol("┃")
            .thumb_style(Style::new().fg(ACCENT)),
        track,
        &mut state,
    );
}

pub(super) fn render_input_field(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    value: &str,
    active: bool,
    placeholder: &str,
) {
    let block = if active {
        theme::focus_panel()
    } else {
        theme::panel()
    }
    .title(format!(" {title} "));
    let content = if value.is_empty() {
        Span::styled(placeholder.to_string(), theme::muted())
    } else {
        Span::raw(value.to_string())
    };
    let line = Line::from(vec![
        Span::styled(" › ", Style::new().fg(ACCENT).bold()),
        content,
        Span::styled(if active { "█" } else { "" }, Style::new().fg(ACCENT)),
    ]);
    frame.render_widget(Paragraph::new(line).block(block), area);
}

pub(super) fn render_empty_state(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    message: &str,
    action: &str,
) {
    let panel_height = 7.min(area.height.max(1));
    let rows = Layout::vertical([
        Constraint::Fill(1),
        Constraint::Length(panel_height),
        Constraint::Fill(1),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(""),
            Line::from(Span::styled(title.to_string(), theme::heading())),
            Line::from(""),
            Line::from(message.to_string()),
            Line::from(Span::styled(action.to_string(), theme::muted())),
        ])
        .alignment(Alignment::Center)
        .wrap(Wrap { trim: false })
        .block(theme::panel()),
        rows[1],
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cell_notes_give_way_before_the_main_text() {
        let cell = || TableCell::new("10-29 00:48").note("  3 h ago");
        assert_eq!(cell().fit(20).to_string(), "10-29 00:48  3 h ago");
        assert_eq!(cell().fit(15).to_string(), "10-29 00:48  3…");
        assert_eq!(cell().fit(8).to_string(), "10-29 0…");
    }

    #[test]
    fn ellipsize_counts_terminal_cells() {
        assert_eq!(ellipsize("short", 8), "short");
        assert_eq!(ellipsize("abcdefgh", 5), "abcd…");
        assert_eq!(ellipsize("中文字符", 5), "中文…");
        assert_eq!(ellipsize("line\nbreak", 20), "line break");
        assert_eq!(ellipsize("abc", 0), "");
    }
}
