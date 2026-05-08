import random

SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']


def new_deck() -> list[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def card_value(card: str) -> int:
    r = card[:-1]
    if r in ('J', 'Q', 'K'):
        return 10
    if r == 'A':
        return 11
    return int(r)


def hand_value(hand: list[str]) -> int:
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c[:-1] == 'A')
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand: list[str]) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def display_hand(hand: list[str], hide_second: bool = False) -> str:
    if hide_second and len(hand) >= 2:
        return f"[{hand[0]}] [?]"
    cards = ' '.join(f"[{c}]" for c in hand)
    return f"{cards} = {hand_value(hand)}"


def dealer_should_hit(hand: list[str]) -> bool:
    return hand_value(hand) < 17
