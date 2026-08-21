use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Stroke, StrokeKind, Vec2};

use crate::model::{Candle, MonitorState, OptionPosition, OptionsMonitorState};

#[derive(Clone)]
struct PriceLine {
    price: f64,
    label: String,
    color: Color32,
    width: f32,
    directional_percent: Option<f64>,
}

pub fn show(ui: &mut egui::Ui, state: &MonitorState, light_mode: bool) {
    let latest_trade_price = state
        .candles
        .last()
        .map(|candle| candle.close)
        .unwrap_or(0.0);
    let lines = futures_price_lines(state, light_mode, latest_trade_price);
    render_chart(
        ui,
        "선물",
        &state.symbol,
        &state.timeframe,
        &state.candles,
        lines,
        "선물 차트 데이터를 기다리는 중…",
        light_mode,
    );
}

pub fn show_options(ui: &mut egui::Ui, state: &OptionsMonitorState, light_mode: bool) {
    let position = state.selected_position();
    let latest_price = state
        .candles
        .last()
        .map(|candle| candle.close)
        .or_else(|| position.map(|position| position.mark_price))
        .unwrap_or(0.0);
    let lines = position
        .map(|position| option_price_lines(position, light_mode, latest_price))
        .unwrap_or_default();
    let empty_message = if position.is_some() {
        "옵션 프리미엄 차트 데이터를 기다리는 중…"
    } else {
        "열린 옵션 포지션 없음"
    };
    render_chart(
        ui,
        "옵션 프리미엄",
        &state.selected_symbol,
        &state.timeframe,
        &state.candles,
        lines,
        empty_message,
        light_mode,
    );
}

#[allow(clippy::too_many_arguments)]
fn render_chart(
    ui: &mut egui::Ui,
    market_label: &str,
    symbol: &str,
    timeframe: &str,
    candles: &[Candle],
    lines: Vec<PriceLine>,
    empty_message: &str,
    light_mode: bool,
) {
    let palette = Palette::new(light_mode);
    let available = ui.available_size();
    let desired = Vec2::new(available.x.max(420.0), available.y.max(250.0));
    let (rect, _) = ui.allocate_exact_size(desired, egui::Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_filled(rect, 8.0, palette.background);

    if candles.len() < 2 {
        painter.text(
            rect.center(),
            Align2::CENTER_CENTER,
            empty_message,
            FontId::proportional(18.0),
            palette.secondary_text,
        );
        return;
    }

    let chart = Rect::from_min_max(
        Pos2::new(rect.left() + 10.0, rect.top() + 28.0),
        Pos2::new(rect.right() - 172.0, rect.bottom() - 22.0),
    );
    let max_visible = ((chart.width() / 7.0).floor() as usize).clamp(40, 220);
    let first = candles.len().saturating_sub(max_visible);
    let candles = &candles[first..];
    // Keep the viewport focused on price action. Distant SL/TP levels are
    // projected to an edge marker instead of shrinking every candle.
    let (low, high) = price_bounds(candles);
    let span = (high - low).max(f64::EPSILON);

    painter.text(
        Pos2::new(chart.left(), rect.top() + 7.0),
        Align2::LEFT_TOP,
        format!(
            "{market_label} · {symbol} · {timeframe} · {}봉",
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

    let mut above_lane = 0usize;
    let mut below_lane = 0usize;
    for line in lines {
        let placement = line_placement(line.price, low, high);
        let (y, prefix, line_start) = match placement {
            LinePlacement::Visible => (y_for(line.price, chart, low, span), "", chart.left()),
            LinePlacement::Above => {
                let lane = above_lane;
                above_lane += 1;
                (
                    chart.top() + 17.0 + lane as f32 * 15.0,
                    "↑ ",
                    chart.right() - 64.0,
                )
            }
            LinePlacement::Below => {
                let lane = below_lane;
                below_lane += 1;
                (
                    chart.bottom() - 17.0 - lane as f32 * 15.0,
                    "↓ ",
                    chart.right() - 64.0,
                )
            }
        };
        painter.line_segment(
            [Pos2::new(line_start, y), Pos2::new(chart.right(), y)],
            Stroke::new(line.width, line.color),
        );
        let distance = line
            .directional_percent
            .map(|percent| format!(" ({percent:+.2}%)"))
            .unwrap_or_default();
        painter.text(
            Pos2::new(chart.right() + 8.0, y),
            Align2::LEFT_CENTER,
            format!(
                "{prefix}{} {}{distance}",
                line.label,
                format_price(line.price)
            ),
            FontId::monospace(11.0),
            line.color,
        );
    }
}

fn y_for(price: f64, chart: Rect, low: f64, span: f64) -> f32 {
    chart.bottom() - ((price - low) / span) as f32 * chart.height()
}

fn price_bounds(candles: &[Candle]) -> (f64, f64) {
    let mut low = f64::INFINITY;
    let mut high = f64::NEG_INFINITY;
    for candle in candles {
        low = low.min(candle.low);
        high = high.max(candle.high);
    }
    if !low.is_finite() || !high.is_finite() || high <= low {
        return (0.0, 1.0);
    }
    let center = (high + low) * 0.5;
    let visible_range = (high - low).max(center.abs() * 0.002);
    let padding = visible_range * 0.10;
    (low - padding, high + padding)
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum LinePlacement {
    Above,
    Visible,
    Below,
}

fn line_placement(price: f64, low: f64, high: f64) -> LinePlacement {
    if price > high {
        LinePlacement::Above
    } else if price < low {
        LinePlacement::Below
    } else {
        LinePlacement::Visible
    }
}

fn futures_price_lines(
    state: &MonitorState,
    light_mode: bool,
    latest_trade_price: f64,
) -> Vec<PriceLine> {
    let palette = Palette::new(light_mode);
    let mut lines = Vec::new();
    if let Some(position) = &state.position {
        if position.entry_price > 0.0 {
            lines.push(PriceLine {
                price: position.entry_price,
                label: "진입".into(),
                color: palette.entry,
                width: 1.5,
                directional_percent: None,
            });
        }
        if latest_trade_price > 0.0 {
            lines.push(PriceLine {
                price: latest_trade_price,
                label: "현재".into(),
                color: palette.current,
                width: 1.0,
                directional_percent: directional_percent(
                    latest_trade_price,
                    position.entry_price,
                    &position.side,
                ),
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
            let label = format!("TP{tp_index}");
            if let Some(existing) = lines.iter_mut().find(|line| {
                line.label.starts_with("TP")
                    && (line.price - price).abs() <= price.abs().max(1.0) * 1e-8
            }) {
                existing.label.push('/');
                existing.label.push_str(&label);
            } else {
                lines.push(PriceLine {
                    price,
                    label,
                    color: palette.take_profit,
                    width: 1.3,
                    directional_percent: directional_percent(
                        price,
                        entry,
                        position_side.unwrap_or(""),
                    ),
                });
            }
        } else if kind.contains("STOP") {
            lines.push(PriceLine {
                price,
                label: "SL".into(),
                color: palette.stop_loss,
                width: 1.5,
                directional_percent: directional_percent(price, entry, position_side.unwrap_or("")),
            });
        } else {
            lines.push(PriceLine {
                price,
                label: "주문".into(),
                color: palette.order,
                width: 1.0,
                directional_percent: directional_percent(price, entry, position_side.unwrap_or("")),
            });
        }
    }
    lines
}

fn option_price_lines(
    position: &OptionPosition,
    light_mode: bool,
    latest_price: f64,
) -> Vec<PriceLine> {
    let palette = Palette::new(light_mode);
    let mut lines = Vec::new();
    if position.entry_price > 0.0 {
        lines.push(PriceLine {
            price: position.entry_price,
            label: "진입".into(),
            color: palette.entry,
            width: 1.5,
            directional_percent: None,
        });
    }
    if latest_price > 0.0 {
        lines.push(PriceLine {
            price: latest_price,
            label: "현재".into(),
            color: palette.current,
            width: 1.0,
            directional_percent: directional_percent(
                latest_price,
                position.entry_price,
                &position.position_side,
            ),
        });
    }
    if let Some(price) = position.hard_stop_price.filter(|price| *price > 0.0) {
        lines.push(PriceLine {
            price,
            label: "SL(봇)".into(),
            color: palette.stop_loss,
            width: 1.5,
            directional_percent: directional_percent(
                price,
                position.entry_price,
                &position.position_side,
            ),
        });
    }
    if let Some(price) = position.hard_target_price.filter(|price| *price > 0.0) {
        lines.push(PriceLine {
            price,
            label: "목표(봇)".into(),
            color: palette.take_profit,
            width: 1.3,
            directional_percent: directional_percent(
                price,
                position.entry_price,
                &position.position_side,
            ),
        });
    }
    if let Some(price) = position.trailing_floor.filter(|price| *price > 0.0) {
        lines.push(PriceLine {
            price,
            label: "추적청산(봇)".into(),
            color: palette.order,
            width: 1.3,
            directional_percent: directional_percent(
                price,
                position.entry_price,
                &position.position_side,
            ),
        });
    }
    lines
}

fn directional_percent(price: f64, entry: f64, side: &str) -> Option<f64> {
    if price <= 0.0 || entry <= 0.0 {
        return None;
    }
    let price_change = (price / entry - 1.0) * 100.0;
    match side {
        "LONG" | "CALL" | "PUT" => Some(price_change),
        "SHORT" => Some(-price_change),
        _ => None,
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn viewport_uses_candles_instead_of_distant_protection_prices() {
        let candles = vec![
            Candle {
                low: 99.0,
                high: 101.0,
                ..Default::default()
            },
            Candle {
                low: 99.5,
                high: 100.5,
                ..Default::default()
            },
        ];

        let (low, high) = price_bounds(&candles);

        assert!(low > 98.0);
        assert!(high < 102.0);
        assert_eq!(line_placement(120.0, low, high), LinePlacement::Above);
        assert_eq!(line_placement(80.0, low, high), LinePlacement::Below);
        assert_eq!(line_placement(100.0, low, high), LinePlacement::Visible);
    }

    #[test]
    fn flat_market_keeps_a_small_readable_minimum_range() {
        let candles = vec![Candle {
            low: 100.0,
            high: 100.01,
            ..Default::default()
        }];

        let (low, high) = price_bounds(&candles);

        assert!(high - low >= 0.049);
    }

    #[test]
    fn current_and_targets_use_direction_aware_entry_return() {
        let long_gain = directional_percent(105.0, 100.0, "LONG").unwrap();
        let short_gain = directional_percent(95.0, 100.0, "SHORT").unwrap();
        let short_loss = directional_percent(105.0, 100.0, "SHORT").unwrap();
        assert!((long_gain - 5.0).abs() < 1e-9);
        assert!((short_gain - 5.0).abs() < 1e-9);
        assert!((short_loss + 5.0).abs() < 1e-9);
        assert_eq!(directional_percent(105.0, 0.0, "LONG"), None);
    }
}
