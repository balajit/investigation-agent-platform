package com.example.battery;

public class Branching {
    public int classify(int value) {
        if (value > 0) {
            return 1;
        }
        return -1;
    }

    public int guarded(int value) {
        try {
            return 10 / value;
        } catch (ArithmeticException e) {
            return 0;
        } finally {
            cleanup();
        }
    }

    private void cleanup() {
    }

    public void risky() {
        throw new IllegalStateException("bad");
    }
}
