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

    public void run(boolean flag, String s) {
        sum(1, 2, 3);
        take(flag && true, -5, s == null ? "d" : "e");
        check(0x10, 010, 0b101);
        execute();
        List<String> items = List.of("a");
        items.forEach(System.out::println);
        Runnable r = () -> sum(7);
        r.run();
    }
}
