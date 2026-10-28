package com.example.generics;

import java.util.List;
import java.util.Map;

class Box<T extends Number & Comparable<T>> {
    Map<String, List<? super T[]>> field;

    <U extends T> U first(List<U> values) {
        return values.get(0);
    }

    void create() {
        Box<T> explicit = null;
        Box<T> inferred = new Box<>();
    }
}
