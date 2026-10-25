"""8080 arithmetic the ported routines share, and the jump a stack reset inside an interrupt makes."""


class Jump(Exception):
    """Raised where the ROM resets the stack inside an interrupt (02D0 after the player's blow-up, 1971 when the rack
    lands): the interrupt is abandoned and `flow`, the main-thread flow it enters, replaces the main thread."""

    def __init__(self, flow):
        super().__init__("main thread replaced")
        self.flow = flow


def bcd_add(a: int, b: int, carry: int = 0) -> tuple[int, int]:
    """ADD (ADC with carry) A,B then DAA, exactly as the 8080 does them: the result and the carry."""
    total = a + b + carry
    aux = (a & 0x0F) + (b & 0x0F) + carry > 0x0F
    cy = total > 0xFF
    r = total & 0xFF
    lo, hi = r & 0x0F, r >> 4
    corr = 0x06 if lo > 9 or aux else 0
    if hi > 9 or cy or (hi >= 9 and lo > 9):
        corr |= 0x60
        cy = True
    return (r + corr) & 0xFF, int(cy)


def rlc(a: int) -> int:
    """RLCA: rotate A left, bit 7 into bit 0."""
    return ((a << 1) | (a >> 7)) & 0xFF
