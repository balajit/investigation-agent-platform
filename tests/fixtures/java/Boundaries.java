package com.example.battery;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

@Configuration
public class Boundaries {
    @Value("${service.timeout}")
    private int timeout;

    @Bean
    public JdbcTemplate jdbcTemplate() {
        return new JdbcTemplate();
    }

    public int loadTimeout() {
        try {
            Class<?> type = Class.forName("com.example.Driver");
            return type.getName().length();
        } catch (ClassNotFoundException e) {
            return -1;
        }
    }

    public long now() {
        return System.currentTimeMillis();
    }
}
