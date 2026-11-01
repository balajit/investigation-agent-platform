package com.example.sep;

public class Separators {
    private String mode = "A";
    public String pick(int v) {
        String s = "x y";
        if (v > 0) { return s; }
        return mode;
    }

    public int count() {
        return 1;
    }
}
