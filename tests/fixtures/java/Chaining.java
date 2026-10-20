package com.example.battery;

public class Chaining {
    private int size = 1;
    private static int count = 0;

    static {
        count = 1;
    }

    {
        size = 2;
    }

    public Chaining() {
        this(4);
    }

    public Chaining(int size) {
        this.size = size;
    }

    public void resize(int size) {
        this.size = size;
    }
}
