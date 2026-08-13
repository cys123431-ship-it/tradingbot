use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Stroke, StrokeKind, Vec2};

use crate::model::{Candle, MonitorState};

#[derive(Clone)]
struct PriceLine {
    price: f64,
    label: String,
    color: Color32,
    width: f32,
}

pub fn show(ui: &mut egui::Ui, state: &MonitorState, light_mode: bool) {
    let palette = Palette::new(light_mode);
    let available = ui.available_size();
    let desired = Vec2::new(available.x.max(420.0), available.y.max(330.0));
    let (rect, _) = ui.allocate_exact_size(desired, egui::Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 8.0, palette.background);

    if state.candles.len() < 2 {
        painter.text(
            rect.center(),
            Align2::CENTER_CENTER,
            "차트 데이터를 기다리는 중…",
            FontId::proportional(18.0),
            palette.secondary_text,
        );
        return;
    }

    let chart = Rect::from_min_max(
        Pos2::new(rect.left() + 10.0, rect.top() + 28.0),
        Pos2::new(rect.right() - 104.0, rect.bottom() - 22.0),
    );
    let max_visible = ((chart.width() / 7.0).floor() as usize).clamp(40, 220);
    let first = state.candles.len().saturating_sub(max_visible);
    let candles = &state.candles[first..];
    let lines = price_lines(state, light_mode);
    let (low, high) = price_bounds(candles, &lines);
    let span = (high - low).max(f64::EPSILON);

    painter.text(
        Pos2::new(chart.left(), rect.top() + 7.0),
        Align2::LEFT_TOP,
        format!(
            "{} · {} · {}봉",
            state.symbol,
            state.timeframe,
            candles.len()
        ),
        FontId::proportional(16.0),
        palette.primary_text,
    );

    for index in 0..=5 {
        let ratio = index as f32 / 5.0;
        let y = egui::lerp(chart.top()..=chart.bottom(), ratio);
        painter.line_segment(
            [Pos2::new(chart.left(), y), Pos2::new(chart.right(), y)],
            Stroke::new(1.0, palette.grid),
        );
        let price = high - span * ratio as f64;
        painter.text(
            Pos2::new(chart.right() + 8.0, y),
            Align2::LEFT_CENTER,
            format_price(price),
            FontId::monospace(11.0),
            palette.secondary_text,
        );
    }

    let candle_width = (chart.width() / candles.len() as f32).max(2.0);
    for (index, candle) in candles.iter().enumerate() {
        let x = chart.left() + (index as f32 + 0.5) * candle_width;
        let high_y = y_for(candle.high, chart, low, span);
        let low_y = y_for(candle.low, chart, low, span);
        let open_y = y_for(candle.open, chart, low, span);
        let close_y = y_for(candle.close, chart, low, span);
        let up = candle.close >= candle.open;
        let color = if up {
            palette.up_candle
        } else {
            palette.down_candle
        };
        painter.line_segment(
            [Pos2::new(x, high_y), Pos2::new(x, low_y)],
            Stroke::new(1.0, color),
        );
        let body = Rect::from_min_max(
            Pos2::new(x - candle_width * 0.32, open_y.min(close_y)),
            Pos2::new(
                x + candle_width * 0.32,
                open_y.max(close_y).max(open_y.min(close_y) + 1.0),
            ),
        );
        if up {
            painter.rect_stroke(body, 0.0, Stroke::new(1.2, color), StrokeKind::Inside);
        } else {
            painter.rect_filled(body, 0.0, color);
        }
    }

    for line in lines {
        if !(low..=high).contains(&line.price) {
            continue;
        }
        let y = y_for(line.price, chart, low, span);
        painter.line_segment(
            [Pos2::new(chart.left(), y), Pos2::new(chart.right(), y)],
            Stroke::new(line.width, line.color),
        );
        painter.text(
            Pos2::new(chart.right() + 8.0, y),
            Align2::LEFT_CENTER,
            format!("{} {}", line.label, format_price(line.price)),
            FontId::monospace(11.0),
            line.color,
        );
    }
}

fn y_for(price: f64, chart: Rect, low: f64, span: f64) -> f32 {
    chart.bottom() - ((price - low) / span) as f32 * chart.height()
}

fn price_bounds(candles: &[Candle], lines: &[PriceLine]) -> (f64, f64) {
    let mut low = f64::INFINITY;
    let mut high = f64::NEG_INFINITY;
    for candle in candles {
        low = low.min(candle.low);
        high = high.max(candle.high);
    }
    for line in lines {
        low = low.min(line.price);
        high = high.max(line.price);
    }
    if !low.is_finite() || !high.is_finite() || high <= low {
        return (0.0, 1.0);
    }
    let padding = (high - low) * 0.08;
    (low - padding, high + padding)
}

fn price_lines(state: &MonitorState, light_mode: bool) -> Vec<PriceLine> {
    let palette = Palette::new(light_mode);
    let mut lines = Vec::new();
    if let Some(position) = &state.position {
        if position.entry_price > 0.0 {
            lines.push(PriceLine {
                price: position.entry_price,
                label: "진입".into(),
                color: palette.entry,
                width: 1.5,
            });
        }
        if position.mark_price > 0.0 {
            lines.push(PriceLine {
                price: position.mark_price,
                label: "현재".into(),
                color: palette.current,
                width: 1.0,
            });
        }
    }
    let position_side = state
        .position
        .as_ref()
        .map(|position| position.side.as_str());
    let entry = state
        .position
        .as_ref()
        .map(|position| position.entry_price)
        .unwrap_or(0.0);
    let mut tp_index = 0;
    for order in &state.orders {
        let Some(price) = order.price.filter(|price| *price > 0.0) else {
            continue;
        };
        let kind = order.order_type.to_ascii_uppercase();
        let is_take_profit = kind.contains("TAKE_PROFIT")
            || (order.reduce_only
                && kind.contains("LIMIT")
                && match position_side {
                    Some("LONG") => price > entry,
                    Some("SHORT") => price < entry,
                    _ => false,
                });
        if is_take_profit {
            tp_index += 1;
            lines.push(PriceLine {
                price,
                label: format!("TP{tp_index}"),
                color: palette.take_profit,
                width: 1.3,
            });
        } else if kind.contains("STOP") {
            lines.push(PriceLine {
                price,
                label: "SL".into(),
                color: palette.stop_loss,
                width: 1.5,
            });
        } else {
            lines.push(PriceLine {
                price,
                label: "주문".into(),
                color: palette.order,
                width: 1.0,
            });
        }
    }
    lines
}

#[derive(Clone, Copy)]
struct Palette {
    background: Color32,
    grid: Color32,
    primary_text: Color32,
    secondary_text: Color32,
    up_candle: Color32,
    down_candle: Color32,
    entry: Color32,
    current: Color32,
    take_profit: Color32,
    stop_loss: Color32,
    order: Color32,
}

impl Palette {
    fn new(light_mode: bool) -> Self {
        if light_mode {
            Self {
                background: Color32::from_rgb(248, 250, 253),
                grid: Color32::from_rgb(218, 224, 232),
                primary_text: Color32::from_rgb(34, 41, 51),
                secondary_text: Color32::from_rgb(100, 110, 125),
                up_candle: Color32::from_rgb(0, 145, 105),
                down_candle: Color32::from_rgb(215, 58, 65),
                entry: Color32::from_rgb(35, 105, 220),
                current: Color32::from_rgb(205, 125, 0),
                take_profit: Color32::from_rgb(0, 140, 80),
                stop_loss: Color32::from_rgb(215, 45, 55),
                order: Color32::from_rgb(145, 70, 170),
            }
        } else {
            Self {
                background: Color32::from_rgb(13, 18, 27),
                grid: Color32::from_rgb(34, 42, 55),
                primary_text: Color32::from_rgb(220, 225, 235),
                secondary_text: Color32::from_gray(125),
                up_candle: Color32::from_rgb(38, 198, 139),
                down_candle: Color32::from_rgb(239, 83, 80),
                entry: Color32::from_rgb(76, 139, 245),
                current: Color32::from_rgb(255, 193, 7),
                take_profit: Color32::from_rgb(56, 201, 126),
                stop_loss: Color32::from_rgb(255, 82, 82),
                order: Color32::from_rgb(186, 104, 200),
            }
        }
    }
}

pub fn format_price(value: f64) -> String {
    let decimals = if value >= 1000.0 {
        2
    } else if value >= 10.0 {
        3
    } else if value >= 1.0 {
        4
    } else if value >= 0.01 {
        6
    } else {
        8
    };
    format!("{value:.decimals$}")
}
