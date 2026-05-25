"""Safe defaults for optional UT Breakout quant hardening layers."""


def default_quant_config():
    return {
        "regime_filter_enabled": False,
        "meta_labeling_enabled": False,
        "meta_sizing_enabled": False,
        "portfolio_risk_enabled": False,
        "drawdown_brake_enabled": False,
        "adaptive_exit_v2_enabled": False,
        "walk_forward_report_enabled": False,
    }
