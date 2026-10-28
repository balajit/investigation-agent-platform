package com.example.app;

import com.example.db.Store;

public class Search {
    private int limit = 10;

    public String find(String name) {
        Store store = new Store();
        store.execute("select * from t");
        store.execute(name);
        store.execute("select " + name);
        return store.fetch(name, limit);
    }

    public String find(String name, int limit) {
        return name;
    }

    public void run() {
        eval("1+1");
        find("x");
    }
}
