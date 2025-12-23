from openai import OpenAI
import os
import json
import pandas as pd
from typing import Dict, Any, Tuple
from .models import AIAnalysisResponse

class AIDecisionEngine:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for AI analysis")
        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def analyze_market(
        self,
        summaries: Dict[str, Dict[str, Any]],
        recent_data: Dict[str, pd.DataFrame],
        trend_context: Dict[str, Any]
    ) -> Tuple[AIAnalysisResponse, str]:
        """Analyze market data for the leveraged ETF pair."""
        prompt = self._build_prompt(summaries, recent_data, trend_context)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a disciplined ETF trading strategist specialising in leveraged ETFs TNA (bull) and TZA (bear). "
                            "Always respond with a compact JSON object matching the requested schema. "
                            "Ensure numbers are decimal values (not strings)."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content
            analysis = json.loads(content)
            return self._to_response_model(analysis), content
        except Exception as exc:
            fallback = {
                "direction": "neutral",
                "confidence": 0,
                "entry_price": None,
                "take_profit": 0.05,
                "early_exit_profit": 0.01,
                "stop_loss": 0.05,
                "use_trailing_stop": False,
                "skip_trade": True,
                "notes": f"AI analysis failed: {exc}"
            }
            return self._to_response_model(fallback), json.dumps(fallback)

    def _build_prompt(
        self,
        summaries: Dict[str, Dict[str, Any]],
        recent_data: Dict[str, pd.DataFrame],
        trend_context: Dict[str, Any]
    ) -> str:
        tna_recent = recent_data.get("TNA").tail(20)
        tza_recent = recent_data.get("TZA").tail(20)
        entry_window_slice = {
            symbol: df.between_time("09:30", "10:00").tail(20).to_dict("records")
            for symbol, df in recent_data.items()
        }

        prompt = f"""
Analyse leveraged ETFs TNA (bull) and TZA (bear) for today's trading session.
Use the following data:

Trend context: {json.dumps(trend_context)[:2000]}

Intraday summaries:
- TNA: {json.dumps(summaries.get('TNA', {}), default=str)}
- TZA: {json.dumps(summaries.get('TZA', {}), default=str)}

Recent 1-min candles (most recent last):
- TNA last 20: {tna_recent[['open','high','low','close','volume','money_flow_volume','volatility_10']].to_dict('records')}
- TZA last 20: {tza_recent[['open','high','low','close','volume','money_flow_volume','volatility_10']].to_dict('records')}

Entry window data (09:30-10:00 ET):
{json.dumps(entry_window_slice, default=str)[:2000]}

Return a JSON object with the exact fields:
{{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": number (0-100),
  "recommended_symbol": "TNA" | "TZA" | null,
  "entry_price": number or null,
  "take_profit": decimal profit target (e.g. 0.05 for 5%),
  "early_exit_profit": decimal (0.005-0.03 when early exit warranted),
  "stop_loss": decimal (0.05 default),
  "use_trailing_stop": boolean,
  "skip_trade": boolean,
  "volume_trend": string (strengthening/weakening/flat),
  "volatility_trend": string,
  "momentum_state": string,
  "long_term_bias": string summarising macro/seasonal biases,
  "early_exit_reason": string or null,
  "trailing_stop_trigger": string or null,
  "notes": concise rationale (<=40 words)
}}

Rules:
- Consider same-day exit mandate (no positions past 15:30 ET).
- If projected move < 1% or signals conflicting or confidence <65, set "skip_trade": true.
- If direction bullish choose TNA, if bearish choose TZA.
- Evaluate viability of 5% profit target and whether trailing stop (1-2%) is justified.
- Identify if early partial exit should be planned (0.5%-3%).
"""
        return prompt

    def _to_response_model(self, analysis: Dict[str, Any]) -> AIAnalysisResponse:
        return AIAnalysisResponse(
            direction=analysis.get("direction", "neutral"),
            confidence=float(analysis.get("confidence", 0.0) or 0.0),
            entry_price=analysis.get("entry_price"),
            take_profit=float(analysis.get("take_profit", 0.05) or 0.05),
            early_exit_profit=float(analysis.get("early_exit_profit", 0.01) or 0.01),
            stop_loss=float(analysis.get("stop_loss", 0.05) or 0.05),
            use_trailing_stop=bool(analysis.get("use_trailing_stop", False)),
            skip_trade=bool(analysis.get("skip_trade", False)),
            recommended_symbol=analysis.get("recommended_symbol"),
            bullish_symbol=analysis.get("bullish_symbol"),
            bearish_symbol=analysis.get("bearish_symbol"),
            volume_trend=analysis.get("volume_trend"),
            volatility_trend=analysis.get("volatility_trend"),
            momentum_state=analysis.get("momentum_state"),
            long_term_bias=analysis.get("long_term_bias"),
            early_exit_reason=analysis.get("early_exit_reason"),
            trailing_stop_trigger=analysis.get("trailing_stop_trigger"),
            notes=analysis.get("notes")
        )

