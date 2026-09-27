class PaymentClient {
  async charge(paymentId, amountCents) {
    const row = await this.db.query("SELECT * FROM payments WHERE id = $1", [paymentId]);
    if (!row) {
      throw new Error(`missing payment ${paymentId}`);
    }
    return `charged:${amountCents}`;
  }

  handle(event) {
    return `payment-event:${event.type || "unknown"}`;
  }
}

function formatReceipt(paymentId) {
  return `receipt:${paymentId}`;
}

module.exports = { PaymentClient, formatReceipt };
