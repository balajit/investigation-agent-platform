package com.example.app;

import java.util.List;

public class Advanced {
    private int base = 40 + 2;

    @Override
    public String toString() {
        return "advanced";
    }

    public int sum(int... values) {
        return values.length;
    }

    public String take(boolean flag, int n, String s) {
        return s;
    }

    public void execute() {
    }

    public void check(int a, int b, int c) {
    }

    public String fmt(float x) {
        return "f";
    }

    public String fmt(double x) {
        return "d";
    }

    public void run(boolean flag, String s) {
        sum(1, 2, 3);
        take(flag && true, -5, s == null ? "d" : "e");
        check(0x10, 010, 0b101);
        fmt(1.0f);
        fmt(1.0);
        execute();
        List<String> items = List.of("a");
        items.forEach(System.out::println);
        Runnable r = () -> sum(7);
        r.run();
    }
}
