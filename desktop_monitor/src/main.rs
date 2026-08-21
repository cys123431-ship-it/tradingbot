#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod chart;
mod model;
mod stream;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use eframe::egui::{self, Color32, FontData, FontDefinitions, FontFamily, RichText};
use model::{MonitorState, StreamEvent};

#[derive(Clone, Copy, PartialEq)]
enum Connection {
    Connecting,
    Connected,
    Disconnected,
}

#[derive(Clone, Copy, PartialEq)]
enum Theme {
    Dark,
    Light,
}

impl Theme {
    fn load() -> Self {
        let Some(path) = theme_path() else {
            return Self::Dark;
        };
        match std::fs::read_to_string(path) {
            Ok(value) if value.trim().eq_ignore_ascii_case("light") => Self::Light,
            _ => Self::Dark,
        }
    }

    fn save(self) {
        let Some(path) = theme_path() else {
            return;
        };
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let value = if self == Self::Light { "light" } else { "dark" };
        let _ = std::fs::write(path, value);
    }

    fn apply(self, ctx: &egui::Context) {
        ctx.set_visuals(if self == Self::Light {
            egui::Visuals::light()
        } else {
            egui::Visuals::dark()
        });
    }

    fn paint_background(self, ui: &egui::Ui) {
        let color = if self == Self::Light {
            Color32::from_rgb(244, 247, 251)
        } else {
            Color32::from_rgb(9, 11, 15)
        };
        ui.painter().rect_filled(ui.max_rect(), 0.0, color);
    }

    fn toggle(&mut self, ctx: &egui::Context) {
        *self = if *self == Self::Dark {
            Self::Light
        } else {
            Self::Dark
        };
        self.apply(ctx);
        self.save();
    }
}

struct MonitorApp {
    rx: Receiver<StreamEvent>,
    stop: Arc<AtomicBool>,
    state: MonitorState,
    connection: Connection,
    last_received: Option<Instant>,
    theme: Theme,
}

impl MonitorApp {
    fn new(cc: &eframe::CreationContext<'_>) -> Self {
        install_korean_font(&cc.egui_ctx);
        let theme = Theme::load();
        theme.apply(&cc.egui_ctx);
        let (tx, rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        stream::spawn(tx, stop.clone());
        Self {
            rx,
            stop,
            state: MonitorState::default(),
            connection: Connection::Connecting,
            last_received: None,
            theme,
        }
    }

    fn receive(&mut self) {
        for event in self.rx.try_iter() {
            match event {
                StreamEvent::Connecting => self.connection = Connection::Connecting,
                StreamEvent::Connected => self.connection = Connection::Connected,
                StreamEvent::Data(snapshot) => {
                    self.state.apply(*snapshot);
                    self.connection = Connection::Connected;
                    self.last_received = Some(Instant::now());
                }
                StreamEvent::Error(error) => {
                    self.connection = Connection::Disconnected;
                    self.state.last_error = Some(error);
                }
            }
        }
        if self
            .last_received
            .is_some_and(|received| received.elapsed() > Duration::from_secs(12))
        {
            self.connection = Connection::Disconnected;
        }
    }

    fn header(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading(RichText::new("TradingBot Monitor").size(22.0));
            ui.separator();
            let (color, label) = match self.connection {
                Connection::Connected => (Color32::from_rgb(50, 205, 120), "서버 연결됨"),
                Connection::Connecting => (Color32::from_rgb(255, 193, 7), "연결 중"),
                Connection::Disconnected => (Color32::from_rgb(255, 82, 82), "재연결 중"),
            };
            ui.colored_label(color, format!("● {label}"));
            ui.separator();
            ui.label(if self.state.exchange_mode.contains("mainnet") {
                RichText::new("선물 MAINNET").color(Color32::from_rgb(255, 112, 112))
            } else {
                RichText::new("선물 TESTNET").color(Color32::from_rgb(100, 181, 246))
            });
            ui.separator();
            ui.label(RichText::new("옵션 MAINNET").color(Color32::from_rgb(190, 125, 255)));
            if self.state.runtime.bot.paused {
                ui.separator();
                ui.colored_label(Color32::from_rgb(255, 193, 7), "선물 PAUSED");
            }
            ui.separator();
            let theme_label = if self.theme == Theme::Dark {
                "☀ Light"
            } else {
                "☾ Dark"
            };
            if ui.button(theme_label).clicked() {
                self.theme.toggle(ui.ctx());
            }
        });
    }

    fn position_panel(&self, ui: &mut egui::Ui) {
        ui.heading("선물 포지션");
        ui.add_space(4.0);
        if let Some(position) = &self.state.position {
            let side_color = if position.side == "LONG" {
                Color32::from_rgb(50, 205, 120)
            } else {
                Color32::from_rgb(255, 82, 82)
            };
            ui.horizontal(|ui| {
                ui.strong(&position.symbol);
                ui.colored_label(side_color, RichText::new(&position.side).strong());
                ui.label(format!("{:.0}x", position.leverage));
            });
            egui::Grid::new("position_grid")
                .num_columns(2)
                .spacing([12.0, 5.0])
                .show(ui, |ui| {
                    row(ui, "구분", &position.source);
                    row(
                        ui,
                        "전략",
                        position
                            .strategy
                            .as_deref()
                            .unwrap_or("수동 또는 식별 불가"),
                    );
                    row(ui, "진입가", &chart::format_price(position.entry_price));
                    row(ui, "마크가", &chart::format_price(position.mark_price));
                    row(ui, "증거금", &format!("{:.2} USDT", position.margin_usdt));
                    row(ui, "포지션", &format!("{:.2} USDT", position.notional_usdt));
                    row(
                        ui,
                        "미실현 PnL",
                        &format!("{:+.2} USDT", position.unrealized_pnl),
                    );
                    row(
                        ui,
                        "ROE",
                        &position
                            .roe_percent
                            .map(|value| format!("{value:+.2}%"))
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "청산가",
                        &position
                            .liquidation_price
                            .map(chart::format_price)
                            .unwrap_or_else(|| "-".into()),
                    );
                });
            ui.add_space(6.0);
            let stops = self
                .state
                .orders
                .iter()
                .filter(|order| order.order_type.contains("STOP"))
                .count();
            let protections = self.state.orders.len();
            let protection_color = if stops > 0 {
                Color32::from_rgb(50, 205, 120)
            } else {
                Color32::from_rgb(255, 82, 82)
            };
            ui.colored_label(
                protection_color,
                format!("보호 주문 {protections}개 · STOP {stops}개"),
            );
        } else {
            ui.colored_label(Color32::from_gray(160), "열린 포지션 없음");
        }
    }

    fn options_position_panel(&self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading("옵션 포지션");
            let (label, color) = if self.state.options.enabled {
                ("신규진입 ON", Color32::from_rgb(50, 205, 120))
            } else {
                ("신규진입 OFF", Color32::from_gray(145))
            };
            ui.colored_label(color, RichText::new(label).small());
        });
        ui.add_space(4.0);
        if let Some(position) = self.state.options.selected_position() {
            let side_color = if position.option_type == "CALL" {
                Color32::from_rgb(50, 205, 120)
            } else {
                Color32::from_rgb(255, 112, 112)
            };
            ui.horizontal_wrapped(|ui| {
                ui.strong(&position.symbol);
                ui.colored_label(
                    side_color,
                    RichText::new(format!(
                        "{} {}",
                        position.position_side, position.option_type
                    ))
                    .strong(),
                );
            });
            if self.state.options.positions.len() > 1 {
                ui.label(
                    RichText::new(format!(
                        "옵션 포지션 총 {}개 · 차트는 위 계약",
                        self.state.options.positions.len()
                    ))
                    .small()
                    .color(ui.visuals().weak_text_color()),
                );
            }
            egui::Grid::new("options_position_grid")
                .num_columns(2)
                .spacing([12.0, 5.0])
                .show(ui, |ui| {
                    row(ui, "구분", &position.source);
                    row(
                        ui,
                        "전략",
                        position
                            .strategy
                            .as_deref()
                            .unwrap_or("수동 또는 식별 불가"),
                    );
                    if !position.underlying.is_empty() {
                        row(ui, "기초자산", &position.underlying);
                    }
                    row(ui, "수량", &format!("{}", position.quantity));
                    row(
                        ui,
                        "진입 프리미엄",
                        &chart::format_price(position.entry_price),
                    );
                    row(ui, "현재 마크", &chart::format_price(position.mark_price));
                    row(
                        ui,
                        "진입 비용",
                        &format!("{:.4} USDT", position.entry_cost_usdt),
                    );
                    row(
                        ui,
                        "현재 가치",
                        &format!("{:.4} USDT", position.premium_value_usdt),
                    );
                    row(
                        ui,
                        "미실현 PnL",
                        &format!("{:+.4} USDT", position.unrealized_pnl),
                    );
                    row(
                        ui,
                        "프리미엄 수익률",
                        &position
                            .return_percent
                            .map(|value| format!("{value:+.2}%"))
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "최고 마크",
                        &position
                            .peak_mark
                            .map(chart::format_price)
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "봇 SL",
                        &position
                            .hard_stop_price
                            .map(chart::format_price)
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "봇 최종목표",
                        &position
                            .hard_target_price
                            .map(chart::format_price)
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "추적청산선",
                        &position
                            .trailing_floor
                            .map(chart::format_price)
                            .unwrap_or_else(|| "미활성".into()),
                    );
                    row(
                        ui,
                        "잔존만기",
                        &position
                            .dte_days
                            .map(|value| format!("{value:.2}일"))
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "IV / Delta",
                        &format!(
                            "{} / {}",
                            option_metric(position.mark_iv, 3),
                            option_metric(position.delta, 3)
                        ),
                    );
                    row(
                        ui,
                        "Gamma / Theta",
                        &format!(
                            "{} / {}",
                            option_metric(position.gamma, 4),
                            option_metric(position.theta, 4)
                        ),
                    );
                    row(ui, "Vega", &option_metric(position.vega, 4));
                    row(
                        ui,
                        "거래소 대조",
                        if position.exchange_verified {
                            "확인됨"
                        } else {
                            "최근 상태 기준"
                        },
                    );
                });
            ui.add_space(5.0);
            let manage_color = if self.state.options.manage_error_streak == 0 {
                Color32::from_rgb(50, 205, 120)
            } else {
                Color32::from_rgb(255, 112, 112)
            };
            ui.colored_label(
                manage_color,
                format!(
                    "소프트웨어 청산 관리 · 최근 {} · 오류 {}회",
                    age_label(self.state.options.last_manage_success_ts),
                    self.state.options.manage_error_streak
                ),
            );
        } else {
            ui.colored_label(Color32::from_gray(160), "열린 옵션 포지션 없음");
        }
        if !self.state.options.last_reason.is_empty() {
            ui.label(
                RichText::new(&self.state.options.last_reason)
                    .small()
                    .color(ui.visuals().weak_text_color()),
            );
        }
        if let Some(error) = &self.state.options.last_error {
            ui.colored_label(Color32::from_rgb(255, 138, 128), error);
        }
    }

    fn strategy_panel(&self, ui: &mut egui::Ui) {
        ui.heading("전략 상태");
        ui.label(
            RichText::new(format!(
                "활성: {}",
                self.state.runtime.bot.active_strategy.to_uppercase()
            ))
            .small(),
        );
        ui.add_space(5.0);
        if self.state.runtime.strategies.is_empty() {
            ui.label("상태 데이터 대기 중…");
            return;
        }
        for strategy in &self.state.runtime.strategies {
            let (dot, color, state_label) = match strategy.state.as_str() {
                "valid" => ("●", Color32::from_rgb(50, 205, 120), "유효"),
                "waiting" => ("●", Color32::from_rgb(255, 193, 7), "대기"),
                "rejected" => ("●", Color32::from_rgb(255, 82, 82), "거절"),
                "off" => ("●", Color32::from_gray(80), "OFF"),
                _ => ("●", Color32::from_gray(155), "미평가"),
            };
            ui.horizontal_wrapped(|ui| {
                ui.colored_label(color, dot);
                ui.strong(&strategy.name);
                ui.label(state_label);
                if let Some(side) = &strategy.side {
                    ui.label(side);
                }
            });
            ui.label(
                RichText::new(&strategy.reason)
                    .small()
                    .color(ui.visuals().weak_text_color()),
            );
            ui.add_space(5.0);
        }
    }

    fn account_panel(&self, ui: &mut egui::Ui) {
        if self.state.position.is_none() && !self.state.runtime.entry_diagnostic.message.is_empty()
        {
            ui.heading("진입 대기 이유");
            if !self.state.runtime.entry_diagnostic.symbol.is_empty() {
                ui.label(
                    RichText::new(&self.state.runtime.entry_diagnostic.symbol)
                        .small()
                        .color(ui.visuals().weak_text_color()),
                );
            }
            ui.label(&self.state.runtime.entry_diagnostic.message);
            ui.add_space(8.0);
        }
        if let Some(status) = self.state.runtime.status_rows.first() {
            ui.heading("봇 상태 요약");
            egui::Grid::new("account_grid")
                .num_columns(2)
                .spacing([12.0, 4.0])
                .show(ui, |ui| {
                    row(ui, "감시 종목", &status.symbol);
                    row(ui, "봇 판단", &status.side);
                    if !status.entry_reason_ko.is_empty() {
                        row(ui, "최근 판단", &status.entry_reason_ko);
                    }
                    row(
                        ui,
                        "계좌",
                        &status
                            .equity
                            .map(|value| format!("{value:.2} USDT"))
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "가용",
                        &status
                            .free_usdt
                            .map(|value| format!("{value:.2} USDT"))
                            .unwrap_or_else(|| "-".into()),
                    );
                    row(
                        ui,
                        "오늘 PnL",
                        &status
                            .daily_pnl
                            .map(|value| format!("{value:+.2} USDT"))
                            .unwrap_or_else(|| "-".into()),
                    );
                });
        }
    }
}

impl Drop for MonitorApp {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

impl eframe::App for MonitorApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.receive();
        self.theme.apply(ui.ctx());
        self.theme.paint_background(ui);
        ui.add_space(5.0);
        self.header(ui);
        ui.separator();

        let available = ui.available_size();
        let status_width = available.x.clamp(330.0, 390.0);
        let chart_width = (available.x - status_width - 12.0).max(420.0);
        let chart_height = ((available.y - 8.0) * 0.5).max(250.0);
        ui.horizontal_top(|ui| {
            ui.allocate_ui_with_layout(
                egui::vec2(chart_width, available.y),
                egui::Layout::top_down(egui::Align::Min),
                |ui| {
                    ui.allocate_ui_with_layout(
                        egui::vec2(chart_width, chart_height),
                        egui::Layout::top_down(egui::Align::Min),
                        |ui| chart::show(ui, &self.state, self.theme == Theme::Light),
                    );
                    ui.separator();
                    ui.allocate_ui_with_layout(
                        egui::vec2(chart_width, chart_height),
                        egui::Layout::top_down(egui::Align::Min),
                        |ui| {
                            chart::show_options(ui, &self.state.options, self.theme == Theme::Light)
                        },
                    );
                },
            );
            ui.separator();
            ui.allocate_ui_with_layout(
                egui::vec2(status_width, available.y),
                egui::Layout::top_down(egui::Align::Min),
                |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        self.position_panel(ui);
                        ui.separator();
                        self.options_position_panel(ui);
                        ui.separator();
                        self.strategy_panel(ui);
                        ui.separator();
                        self.account_panel(ui);
                        if let Some(error) = &self.state.last_error {
                            ui.separator();
                            ui.colored_label(Color32::from_rgb(255, 138, 128), error);
                        }
                        ui.add_space(8.0);
                        ui.label(
                            RichText::new(format!(
                                "서버 상태: {}",
                                if self.state.runtime.updated_at.is_empty() {
                                    "대기 중"
                                } else {
                                    &self.state.runtime.updated_at
                                }
                            ))
                            .small()
                            .color(ui.visuals().weak_text_color()),
                        );
                    });
                },
            );
        });
        ui.ctx().request_repaint_after(Duration::from_millis(250));
    }
}

fn row(ui: &mut egui::Ui, label: &str, value: &str) {
    ui.label(RichText::new(label).color(ui.visuals().weak_text_color()));
    ui.label(value);
    ui.end_row();
}

fn option_metric(value: Option<f64>, decimals: usize) -> String {
    value
        .map(|value| format!("{value:.decimals$}"))
        .unwrap_or_else(|| "-".into())
}

fn age_label(epoch_seconds: f64) -> String {
    if epoch_seconds <= 0.0 {
        return "확인 대기".into();
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(epoch_seconds);
    let age = (now - epoch_seconds).max(0.0);
    if age < 90.0 {
        format!("{age:.0}초 전")
    } else {
        format!("{:.1}분 전", age / 60.0)
    }
}

fn theme_path() -> Option<std::path::PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(|root| {
        std::path::PathBuf::from(root)
            .join("TradingBotMonitor")
            .join("theme.txt")
    })
}

fn install_korean_font(ctx: &egui::Context) {
    let candidates = [
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\malgunsl.ttf",
    ];
    let Some(bytes) = candidates.iter().find_map(|path| std::fs::read(path).ok()) else {
        return;
    };
    let mut fonts = FontDefinitions::default();
    fonts
        .font_data
        .insert("malgun".into(), Arc::new(FontData::from_owned(bytes)));
    for family in [FontFamily::Proportional, FontFamily::Monospace] {
        fonts
            .families
            .entry(family)
            .or_default()
            .insert(0, "malgun".into());
    }
    ctx.set_fonts(fonts);
}

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("TradingBot Monitor")
            .with_inner_size([1280.0, 760.0])
            .with_min_inner_size([900.0, 580.0]),
        renderer: eframe::Renderer::Glow,
        ..Default::default()
    };
    eframe::run_native(
        "TradingBot Monitor",
        options,
        Box::new(|cc| Ok(Box::new(MonitorApp::new(cc)))),
    )
}
