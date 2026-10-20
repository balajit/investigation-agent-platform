package com.example.battery;

public sealed interface Shape permits Circle, Square {
    double area();
}

record Circle(double radius) implements Shape {
    public double area() {
        return 3.14 * radius * radius;
    }
}

record Square(double side) implements Shape {
    public double area() {
        return side * side;
    }
}

public class Shapes {
    public String describe(Object value) {
        if (value instanceof Circle c) {
            return "circle";
        }
        return switch (value) {
            case Square s -> "square";
            case null -> "none";
            default -> "other";
        };
    }
}
