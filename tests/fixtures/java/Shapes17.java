package com.example.shapes;

public record Point(int x, int y) {}

public sealed interface Shape permits Circle, Square {
    double area();
}

final class Circle implements Shape {
    private final double radius;

    Circle(double radius) {
        this.radius = radius;
    }

    @Override
    public double describe(Object o) {
        if (o instanceof Circle c) {
            return c.radius;
        }
        return -1.0;
    }

    @Override
    public double area() {
        return switch ((int) radius) {
            case 0 -> 0.0;
            case 1 -> 3.14;
            default -> {
                yield Math.PI * radius * radius;
            }
        };
    }
}

final class Square implements Shape {
    private final double side;

    Square(double side) {
        this.side = side;
    }

    @Override
    public double area() {
        return side * side;
    }

    public double broken( {
        return -1.0;
    }
}
