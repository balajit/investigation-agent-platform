// JGF-T09 staged fixture (GATED: assertions enabled when the Spring gate lifts).
package com.example.boundaries;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Profile("prod")
@Component
public class Boundaries17 {
    private final String endpoint;

    public Boundaries17(@Value("${service.endpoint:https://default.internal}") String endpoint) {
        this.endpoint = endpoint;
    }

    @org.springframework.web.bind.annotation.GetMapping(path = {"/orders", "/orders/{id}"})
    public String route(String id) {
        return endpoint + "#{T(java.lang.Math).random()}";
    }
}
