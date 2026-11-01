package com.sample.orders;

public class OrderServiceHandler {
    public enum Mode {
        STANDARD,
        EXPRESS
    }

    public interface Limits {
        int LIMIT = 5;
    }

    private Mode mode = Mode.STANDARD;

    public Mode mode() {
        return mode;
    }

    public static class Helper {
        public static String describe() {
            return "helper";
        }
    }
}
