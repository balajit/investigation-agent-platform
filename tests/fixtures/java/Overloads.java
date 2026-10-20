package com.example.battery;

public class Overloads {
    public void handle(int value) {
    }

    public void handle(long value) {
    }

    public void handle(String... values) {
    }

    public void run() {
        Runner runner = new Runner();
        runner.execute(3);
    }

    static class Runner {
        void execute(int value) {
        }

        void execute(Integer value) {
        }
    }
}
