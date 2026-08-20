"""
Model Blue IB — Position Sizer (US ETFs / CFDs, IBKR)
=====================================================

YEH KYA HAI
-----------
Strategy engine (Pine) sirf yeh batata hai ki trade ka SHAPE kya hai — kaunse legs,
kis direction mein, aur kis weight par. Kitna paisa lagana hai woh OEMS decide karta
hai. Yeh file un dono ko jodti hai: weight + paisa  ->  actual order quantity.

NSE wale helper se ek hi farak hai: yahan LOTS nahi hain. ETF/CFD shares mein trade
hote hain, isliye quantity = notional / price. Bas.

PAYLOAD SE YAHAN TAK
--------------------
Pine ka JSON aisa aata hai:
    buckets[i].legs[0]  ->  underlying, side, weight, price
Har bucket ka ek Leg banao (neeche wala dataclass), aur signal ka direction +1/-1
uthao. Baaki sab yeh function kar dega.

DO RULES JO KABHI MAT TODNA
---------------------------
1. Weights SIGNED hote hain. Positive = spread ka long side, negative = short side.
   Har leg ka final side = sign(weight) x direction.
2. Pair mein PEHLA leg base leg hai. Capital usi par anchor hota hai. Order ulta
   kiya to hedge ulta ho jayega.
"""

from dataclasses import dataclass, field
from typing import List

MIN_ORDER_NOTIONAL = 100.0   # $ — isse chhota order bhejne ka koi matlab nahi


# ------------------------------- Input -------------------------------

@dataclass
class Leg:
    symbol: str          # "SPY"
    weight: float        # SIGNED + normalised (sab |weight| ka sum = 1)
    price: float         # reference price, payload se ya fresh quote se


@dataclass
class Signal:
    trade_id: str        # entry aur exit dono par same — isi se position close hoti hai
    strategy: str        # "model_blue" = 2 legs, "model_blue_plus" = 3+ legs
    direction: int       # +1 = spread long, -1 = spread short
    legs: List[Leg]      # pair mein base leg PEHLE


# ------------------------------- Output -------------------------------

@dataclass
class SizedLeg:
    symbol: str
    side: str            # "BUY" / "SELL"
    quantity: float      # fractional shares (4 decimal tak)
    price: float
    notional: float


@dataclass
class Result:
    trade_id: str
    status: str          # "SIZED" ya "REJECTED"
    reason: str          # REJECTED hone ki wajah, warna khaali
    committed: float     # OEMS ne jitna paisa diya
    gross: float         # market mein actually kitna exposure gaya
    legs: List[SizedLeg] = field(default_factory=list)


# ----------------------------- Main function ---------------------------

def size_trade(signal: Signal, committed: float) -> Result:
    """
    committed = OEMS ne is trade ke liye jo capital allocate kiya (allocation logic
                OEMS ka kaam hai, yahan nahi). Pair mein yeh BASE leg ka notional hai;
                basket mein poore basket ka.
    """
    legs = signal.legs
    is_pair = signal.strategy == "model_blue"     # exact match — "Model Blue" likha to basket branch chala jayega
    total_w = sum(abs(l.weight) for l in legs)

    # --- Sanity checks: galat input jaldi pakdo, silently mat sizing karo ---
    if len(legs) < 2 or (is_pair and len(legs) != 2):
        return _reject(signal, f"{len(legs)} legs mile — pair ko 2 chahiye, basket ko 2+")
    if committed <= 0:
        return _reject(signal, "committed capital zero hai")
    if total_w == 0:
        return _reject(signal, "sab weights zero hain")

    # --- Step 1: har leg ko kitna paisa? Yahin pair aur basket alag hote hain ---
    if is_pair:
        # Base leg (legs[0]) ko poora committed; doosri leg uske ratio mein.
        base_w = abs(legs[0].weight)
        targets = [committed * abs(l.weight) / base_w for l in legs]
    else:
        # Basket: committed ko sab legs mein weight ke hisaab se baant do.
        targets = [committed * abs(l.weight) / total_w for l in legs]

    # --- Step 2: paisa -> quantity. Yahan lots nahi, seedha shares. ---
    sized: List[SizedLeg] = []
    for l, target in zip(legs, targets):
        if l.price <= 0:
            return _reject(signal, f"{l.symbol} ka price {l.price} — invalid")

        # Fractional quantity — 4 decimal tak, jitna IB accept karta hai. Isliye hedge
        # ratio exactly wahi milta hai jo weights mein aaya; koi rounding error nahi,
        # aur koi hedge-error check ki zaroorat nahi.
        qty = round(target / l.price, 4)
        notional = qty * l.price

        # Ek hi check kaafi hai: qty 0 nikli to notional bhi 0 hoga, wahi pakad lega.
        if notional < MIN_ORDER_NOTIONAL:
            return _reject(signal, f"{l.symbol}: ${notional:.0f} ka order — minimum ${MIN_ORDER_NOTIONAL:.0f} chahiye, account chhota hai")

        # Side yahan banti hai. Weight shape deta hai, direction bataata hai kis taraf.
        side = "BUY" if (l.weight * signal.direction) > 0 else "SELL"
        sized.append(SizedLeg(l.symbol, side, qty, l.price, notional))

    return Result(
        trade_id=signal.trade_id,
        status="SIZED",
        reason="",
        committed=committed,
        gross=sum(s.notional for s in sized),
        legs=sized,
    )


def _reject(signal: Signal, reason: str) -> Result:
    # REJECT bhi ek valid jawab hai. Chup-chaap galat size bhejne se accha hai.
    return Result(signal.trade_id, "REJECTED", reason, 0.0, 0.0, [])


# -------------------------------- Example -----------------------------
# EXIT ka koi sizing nahi hota. Pine sirf {"action":"CLOSE","trade_id":...} bhejta hai
# aur OEMS us trade_id par jo position khuli hai use band kar deta hai. Bas.

if __name__ == "__main__":
    sig = Signal(
        trade_id="MBG-SPY-IVV-20260813T0935",
        strategy="model_blue",
        direction=+1,                                  # spread long
        legs=[
            Leg("SPY", weight=+0.503, price=642.10),   # base leg — hamesha pehle
            Leg("IVV", weight=-0.497, price=645.80),   # hedge
        ],
    )

    r = size_trade(sig, committed=25_000)
    print(f"{r.status} {r.reason}".rstrip())
    for leg in r.legs:
        print(f"   {leg.side:4} {leg.symbol:5} {leg.quantity:>10}  (${leg.notional:,.0f})")
    if r.status == "SIZED":
        print(f"   committed ${r.committed:,.0f}   gross ${r.gross:,.0f}")
