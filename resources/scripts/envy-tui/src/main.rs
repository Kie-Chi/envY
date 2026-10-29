mod app;
mod backend;
mod model;
mod theme;
mod ui;

use std::{
    io::{self, Stdout, Write},
    process::Command,
    sync::mpsc,
    time::Duration,
};

use app::App;
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use model::WorkflowAction;
use ratatui::{backend::CrosstermBackend, Terminal};

const POLL_INTERVAL: Duration = Duration::from_millis(80);

fn run_workflow(
    terminal: &mut Terminal<CrosstermBackend<Stdout>>,
    action: &WorkflowAction,
) -> io::Result<bool> {
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        DisableBracketedPaste,
        DisableMouseCapture,
        LeaveAlternateScreen
    )?;
    terminal.show_cursor()?;

    println!("\n== {} ==\n", action.title());
    let (program, args) = action.command(backend::envy_binary());
    let result = Command::new(program).args(args).status();
    let success = result.as_ref().is_ok_and(|status| status.success());
    if let Err(error) = &result {
        eprintln!("Could not start envY workflow: {error}");
    }
    println!(
        "\nWorkflow {}. Press Enter to return to the envY TUI.",
        if success { "completed" } else { "failed" }
    );
    io::stdout().flush()?;
    let mut input = String::new();
    io::stdin().read_line(&mut input)?;

    enable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste
    )?;
    terminal.clear()?;
    Ok(success)
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
    let (tx, rx) = mpsc::channel();
    let mut app = App::new(tx, rx);
    app.load_current(false);
    while !app.should_quit {
        let page_height = ui::page_height(&app, terminal.size()?.height);
        app.clamp_scroll(page_height);
        terminal.draw(|frame| ui::draw(frame, &mut app))?;
        if event::poll(POLL_INTERVAL)? {
            match event::read()? {
                Event::Key(key) => app.handle_key(key),
                Event::Mouse(mouse) => app.handle_mouse(mouse),
                Event::Paste(value) => app.handle_paste(&value),
                _ => {}
            }
        }
        app.receive_messages();
        if let Some(action) = app.take_workflow_action() {
            let success = run_workflow(terminal, &action)?;
            app.workflow_finished(&action, success);
        }
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(
        stdout,
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste
    )?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    let result = run_app(&mut terminal);
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        DisableBracketedPaste,
        DisableMouseCapture,
        LeaveAlternateScreen
    )?;
    terminal.show_cursor()?;
    result?;
    Ok(())
}
