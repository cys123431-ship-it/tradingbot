use serde_json::Value;

use super::binance::BinanceError;

const OUTCOME_UNKNOWN_CLASSES: &[&str] = &[
    "TimeoutError",
    "RequestTimeout",
    "NetworkError",
    "ExchangeNotAvailable",
    "ConnectionError",
];

#[derive(Debug, Clone)]
pub struct ExchangeError {
    pub class: String,
    pub message: String,
    pub code: Option<i64>,
    pub outcome_unknown: bool,
}

impl std::fmt::Display for ExchangeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ExchangeError {}

impl ExchangeError {
    pub fn new(class: &str, message: impl Into<String>) -> Self {
        Self {
            class: class.to_string(),
            message: message.into(),
            code: None,
            outcome_unknown: OUTCOME_UNKNOWN_CLASSES.contains(&class),
        }
    }

    pub fn from_binance(err: &BinanceError) -> Self {
        let unknown = err.outcome_unknown();
        let (class, message, code) = match err {
            BinanceError::RateLimited { status, .. } => (
                "DDoSProtection".to_string(),
                format!("binance {status} "),
                None,
            ),
            BinanceError::Network { sent, message } => {
                let class = if *sent {
                    "RequestTimeout"
                } else {
                    "NetworkError"
                };
                (
                    class.to_string(),
                    format!("binance network: {message}"),
                    None,
                )
            }
            BinanceError::Http { status, body } => {
                let class = if *status >= 500 {
                    "ExchangeNotAvailable"
                } else {
                    "ExchangeError"
                };
                (class.to_string(), format!("binance {status} {body}"), None)
            }
            BinanceError::Decode(m) => ("ExchangeError".to_string(), m.clone(), None),
            BinanceError::Invalid(m) => ("InvalidOrder".to_string(), m.clone(), None),
            BinanceError::MissingCredentials => (
                "AuthenticationError".to_string(),
                "binance missing credentials".to_string(),
                None,
            ),
            BinanceError::Api {
                status,
                code: c,
                msg,
            } => {
                let (class, message) = classify_api(*status, *c, msg);
                (class, message, Some(*c))
            }
        };
        Self {
            class,
            message,
            code,
            outcome_unknown: unknown,
        }
    }
}

impl From<BinanceError> for ExchangeError {
    fn from(err: BinanceError) -> Self {
        ExchangeError::from_binance(&err)
    }
}

/// Builds the compressed `{"code":C,"msg":"M"}` body as Binance sends it.
fn compact_body(code: i64, msg: &str) -> String {
    let escaped = msg.replace('"', "\\\"");
    format!("{{\"code\":{code},\"msg\":\"{escaped}\"}}")
}

/// ccxt `binance.handle_errors` for a fapi `{"code","msg"}` error, in ccxt's order.
fn classify_api(status: u16, code: i64, msg: &str) -> (String, String) {
    let body = compact_body(code, msg);
    let invalid = |text: &str| ("InvalidOrder".to_string(), format!("binance {text} {body}"));
    if status >= 400 {
        if body.contains("Price * QTY is zero or less") {
            return invalid("order cost = amount * price is zero or less");
        }
        if body.contains("LOT_SIZE") {
            return invalid("order amount should be evenly divisible by lot size");
        }
        if body.contains("PRICE_FILTER") {
            return invalid(
                "order price is invalid, i.e. exceeds allowed price precision, exceeds min price or max price limits or is invalid value in general, use self.price_to_precision(symbol, amount)",
            );
        }
    }
    // Message matches first (feedback is "binance <msg>").
    let by_message = linear_exact_lookup(msg)
        .or_else(|| exact_lookup(msg))
        .or_else(|| linear_broad_lookup(msg))
        .or_else(|| broad_lookup(msg));
    if let Some(class) = by_message {
        return (class.to_string(), format!("binance {msg}"));
    }
    let feedback = format!("binance {body}");
    if code == 200 || code == 0 {
        return ("ExchangeError".to_string(), feedback);
    }
    // ccxt raises DDoSProtection for -2015 only after a successful login; the
    // conservative choice here is the mapped AuthenticationError (locks entries).
    if msg == "No need to change margin type." {
        return ("MarginModeAlreadySet".to_string(), feedback);
    }
    let key = code.to_string();
    let class = linear_exact_lookup(&key)
        .or_else(|| exact_lookup(&key))
        .unwrap_or("ExchangeError");
    (class.to_string(), feedback)
}

#[derive(Clone, Copy)]
struct Entry {
    key: &'static str,
    class: &'static str,
}

const LINEAR_EXACT: &[Entry] = &[
    e("-1005", "PermissionDenied"),
    e("-1008", "OperationFailed"),
    e("-1011", "PermissionDenied"),
    e("-1023", "BadRequest"),
    e("-1099", "AuthenticationError"),
    e("-1109", "PermissionDenied"),
    e("-1110", "BadRequest"),
    e("-1113", "BadRequest"),
    e("-1122", "BadRequest"),
    e("-1126", "BadSymbol"),
    e("-1136", "BadRequest"),
    e("-2012", "OperationFailed"),
    e("-2016", "OperationRejected"),
    e("-2017", "PermissionDenied"),
    e("-2018", "InsufficientFunds"),
    e("-2019", "InsufficientFunds"),
    e("-2020", "OperationFailed"),
    e("-2021", "OrderImmediatelyFillable"),
    e("-2022", "InvalidOrder"),
    e("-2023", "OperationFailed"),
    e("-2024", "InsufficientFunds"),
    e("-2025", "OperationRejected"),
    e("-2026", "InvalidOrder"),
    e("-2027", "OperationRejected"),
    e("-2028", "OperationRejected"),
    e("-4063", "BadRequest"),
    e("-4064", "BadRequest"),
    e("-4065", "BadRequest"),
    e("-4066", "BadRequest"),
    e("-4069", "BadRequest"),
    e("-4070", "BadRequest"),
    e("-4071", "BadRequest"),
    e("-4072", "OperationRejected"),
    e("-4073", "BadRequest"),
    e("-4074", "OperationRejected"),
    e("-4075", "BadRequest"),
    e("-4076", "OperationRejected"),
    e("-4077", "OperationRejected"),
    e("-4078", "OperationFailed"),
    e("-4079", "BadRequest"),
    e("-4080", "PermissionDenied"),
    e("-4081", "BadRequest"),
    e("-4085", "BadRequest"),
    e("-4087", "PermissionDenied"),
    e("-4088", "PermissionDenied"),
    e("-4114", "BadRequest"),
    e("-4115", "BadRequest"),
    e("-4116", "InvalidOrder"),
    e("-4117", "OperationRejected"),
    e("-4118", "OperationRejected"),
    e("-4131", "OperationRejected"),
    e("-4136", "InvalidOrder"),
    e("-4140", "BadRequest"),
    e("-4141", "OperationRejected"),
    e("-4144", "BadSymbol"),
    e("-4164", "InvalidOrder"),
    e("-4165", "BadRequest"),
    e("-4167", "BadRequest"),
    e("-4168", "BadRequest"),
    e("-4169", "OperationRejected"),
    e("-4170", "OperationRejected"),
    e("-4171", "OperationRejected"),
    e("-4172", "OperationRejected"),
    e("-4183", "BadRequest"),
    e("-4184", "BadRequest"),
    e("-4192", "PermissionDenied"),
    e("-4202", "PermissionDenied"),
    e("-4203", "PermissionDenied"),
    e("-4205", "PermissionDenied"),
    e("-4206", "PermissionDenied"),
    e("-4208", "OperationRejected"),
    e("-4209", "OperationRejected"),
    e("-4210", "BadRequest"),
    e("-4211", "BadRequest"),
    e("-4400", "PermissionDenied"),
    e("-4401", "PermissionDenied"),
    e("-4402", "PermissionDenied"),
    e("-4403", "PermissionDenied"),
    e("-5021", "OrderNotFillable"),
    e("-5022", "OrderNotFillable"),
    e("-5024", "OperationRejected"),
    e("-5025", "OperationRejected"),
    e("-5026", "OperationRejected"),
    e("-5027", "OperationRejected"),
    e("-5028", "BadRequest"),
    e("-5037", "BadRequest"),
    e("-5038", "BadRequest"),
    e("-5039", "BadRequest"),
    e("-5040", "BadRequest"),
    e("-5041", "OperationFailed"),
];

const EXACT: &[Entry] = &[
    e("-1000", "OperationFailed"),
    e("-1001", "OperationFailed"),
    e("-1002", "AuthenticationError"),
    e("-1003", "RateLimitExceeded"),
    e("-1004", "OperationRejected"),
    e("-1006", "OperationFailed"),
    e("-1007", "RequestTimeout"),
    e("-1010", "OperationFailed"),
    e("-1013", "BadRequest"),
    e("-1014", "InvalidOrder"),
    e("-1015", "RateLimitExceeded"),
    e("-1016", "BadRequest"),
    e("-1020", "BadRequest"),
    e("-1021", "InvalidNonce"),
    e("-1022", "AuthenticationError"),
    e("-1100", "BadRequest"),
    e("-1101", "BadRequest"),
    e("-1102", "BadRequest"),
    e("-1103", "BadRequest"),
    e("-1104", "BadRequest"),
    e("-1105", "BadRequest"),
    e("-1106", "BadRequest"),
    e("-1108", "BadSymbol"),
    e("-1111", "BadRequest"),
    e("-1112", "OperationFailed"),
    e("-1114", "BadRequest"),
    e("-1115", "BadRequest"),
    e("-1116", "BadRequest"),
    e("-1117", "BadRequest"),
    e("-1118", "BadRequest"),
    e("-1119", "BadRequest"),
    e("-1120", "BadRequest"),
    e("-1121", "BadSymbol"),
    e("-1125", "AuthenticationError"),
    e("-1127", "BadRequest"),
    e("-1128", "BadRequest"),
    e("-1130", "BadRequest"),
    e("-2010", "InvalidOrder"),
    e("-2011", "OrderNotFound"),
    e("-20121", "BadSymbol"),
    e("-20124", "BadRequest"),
    e("-2013", "OrderNotFound"),
    e("-20130", "BadRequest"),
    e("-20132", "BadRequest"),
    e("-2014", "AuthenticationError"),
    e("-2015", "AuthenticationError"),
    e("-20194", "BadRequest"),
    e("-20195", "BadRequest"),
    e("-20196", "BadRequest"),
    e("-20198", "OperationRejected"),
    e("-20204", "BadRequest"),
    e("-4000", "InvalidOrder"),
    e("-4001", "BadRequest"),
    e("-4002", "BadRequest"),
    e("-4003", "BadRequest"),
    e("-4004", "BadRequest"),
    e("-4005", "BadRequest"),
    e("-4006", "BadRequest"),
    e("-4007", "BadRequest"),
    e("-4008", "BadRequest"),
    e("-4009", "BadRequest"),
    e("-4010", "BadRequest"),
    e("-4011", "BadRequest"),
    e("-4012", "BadRequest"),
    e("-4013", "BadRequest"),
    e("-4014", "BadRequest"),
    e("-4015", "BadRequest"),
    e("-4016", "BadRequest"),
    e("-4017", "BadRequest"),
    e("-4018", "BadRequest"),
    e("-4019", "OperationRejected"),
    e("-4020", "BadRequest"),
    e("-4021", "BadRequest"),
    e("-4022", "BadRequest"),
    e("-4023", "BadRequest"),
    e("-4024", "BadRequest"),
    e("-4025", "BadRequest"),
    e("-4026", "BadRequest"),
    e("-4027", "BadRequest"),
    e("-4028", "BadRequest"),
    e("-4029", "BadRequest"),
    e("-4030", "BadRequest"),
    e("-4031", "BadRequest"),
    e("-4032", "OperationRejected"),
    e("-4033", "BadRequest"),
    e("-4044", "BadRequest"),
    e("-4045", "OperationRejected"),
    e("-4046", "OperationRejected"),
    e("-4047", "OperationRejected"),
    e("-4048", "OperationRejected"),
    e("-4049", "BadRequest"),
    e("-4050", "InsufficientFunds"),
    e("-4051", "InsufficientFunds"),
    e("-4052", "OperationRejected"),
    e("-4053", "BadRequest"),
    e("-4054", "OperationRejected"),
    e("-4055", "BadRequest"),
    e("-4056", "AuthenticationError"),
    e("-4057", "AuthenticationError"),
    e("-4058", "BadRequest"),
    e("-4059", "OperationRejected"),
    e("-4060", "BadRequest"),
    e("-4061", "OperationRejected"),
    e("-4062", "BadRequest"),
    e("-4067", "OperationRejected"),
    e("-4068", "OperationRejected"),
    e("-4082", "BadRequest"),
    e("-4083", "OperationRejected"),
    e("-4084", "BadRequest"),
    e("-4086", "BadRequest"),
    e("-4104", "BadRequest"),
    e("-4135", "BadRequest"),
    e("-4137", "BadRequest"),
    e("-4138", "BadRequest"),
    e("-4139", "BadRequest"),
    e("-4142", "OrderImmediatelyFillable"),
    e("API key does not exist", "AuthenticationError"),
    e(
        "Account has insufficient balance for requested action.",
        "InsufficientFunds",
    ),
    e("Limit orders require GTC for self phase.", "BadRequest"),
    e("Market is closed.", "MarketClosed"),
    e(
        "Order would immediately match and take.",
        "OrderImmediatelyFillable",
    ),
    e(
        "Order would trigger immediately.",
        "OrderImmediatelyFillable",
    ),
    e("Rest API trading is not enabled.", "PermissionDenied"),
    e(
        "Stop price would trigger immediately.",
        "OrderImmediatelyFillable",
    ),
    e("System abnormality", "OperationFailed"),
    e("System is under maintenance.", "OnMaintenance"),
    e(
        "This account may not place or cancel orders.",
        "PermissionDenied",
    ),
    e(
        "This action is disabled on self account.",
        "AccountSuspended",
    ),
    e(
        "This order type is not hasattr(self, possible) trading phase.",
        "BadRequest",
    ),
    e(
        "This symbol is not permitted for self account.",
        "PermissionDenied",
    ),
    e(
        "This symbol is restricted for self account.",
        "PermissionDenied",
    ),
    e(
        "This type of sub-account exceeds the maximum number limit",
        "OperationRejected",
    ),
    e(
        "Too many requests. Please try again later.",
        "RateLimitExceeded",
    ),
    e(
        "You are not authorized to execute self request.",
        "PermissionDenied",
    ),
    e("You don't have permission.", "PermissionDenied"),
];

const _LINEAR_BROAD: &[Entry] = &[];

const BROAD: &[Entry] = &[
    e("MAX_POSITION", "BadRequest"),
    e("PERCENT_PRICE_BY_SIDE", "InvalidOrder"),
    e("has no operation privilege", "PermissionDenied"),
];

const fn e(key: &'static str, class: &'static str) -> Entry {
    Entry { key, class }
}

fn linear_exact_lookup(key: &str) -> Option<&'static str> {
    LINEAR_EXACT
        .iter()
        .find(|entry| entry.key == key)
        .map(|entry| entry.class)
}

fn exact_lookup(key: &str) -> Option<&'static str> {
    EXACT
        .iter()
        .find(|entry| entry.key == key)
        .map(|entry| entry.class)
}

fn linear_broad_lookup(msg: &str) -> Option<&'static str> {
    _LINEAR_BROAD
        .iter()
        .find(|entry| msg.contains(entry.key))
        .map(|entry| entry.class)
}

fn broad_lookup(msg: &str) -> Option<&'static str> {
    BROAD
        .iter()
        .find(|entry| msg.contains(entry.key))
        .map(|entry| entry.class)
}

/// Classifies a ccxt-shaped error body `Value` (kept for callers that have one).
pub fn classify_body(v: &Value) -> ExchangeError {
    let code = v.get("code").and_then(|c| {
        c.as_i64()
            .or_else(|| c.as_str().and_then(|s| s.parse::<i64>().ok()))
    });
    let msg = v
        .get("msg")
        .and_then(|m| m.as_str())
        .unwrap_or_default()
        .to_string();
    let body = match code {
        Some(c) => compact_body(c, &msg),
        None => msg,
    };
    ExchangeError::new("ExchangeError", format!("binance {body}"))
}
