#!/usr/bin/env python3
"""Build the synthetic Spring calibration checkout (untracked fixture data).

Creates data/code-repos/samplecorp/orders-internal: a minimal multi-module
Gradle + Java tree with technology-generic names (service impl, filter,
handler + callers, legacy mapper) and a 3-commit git history so the
Tranche B battery exercises symbol lookup, caller enumeration, and commit
history against respondent-free data.

Idempotent: refuses to run when the target dir already holds a git repo.
Git identity is passed per-command (-c) and HOME is pointed at a temp dir
so the operator's global gitconfig is never touched.

Usage:
  uv run python scripts/build-sample-checkout.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

REPO = "data/code-repos/samplecorp/orders-internal"
GIT_IDENTITY = ["-c", "user.name=iap-calibration", "-c", "user.email=iap-calibration@example.com"]

FILES_V1 = {
    "settings.gradle": "rootProject.name = 'orders-internal'\ninclude 'service', 'commons'\n",
    "service/build.gradle": "plugins { id 'java' }\n",
    "commons/build.gradle": "plugins { id 'java' }\n",
    "service/src/main/java/com/sample/orders/service/OrderServiceImpl.java": """package com.sample.orders.service;

import com.sample.orders.legacy.LegacyCodeMapper;

/** Spring service implementation: determines order eligibility. */
public class OrderServiceImpl {

    private final LegacyCodeMapper mapper = new LegacyCodeMapper();

    public boolean isEligible(String orderType, String customerSegment) {
        String normalized = mapper.normalize(orderType);
        if ("STANDARD".equals(normalized) && "BULK".equals(customerSegment)) {
            return false;
        }
        return true;
    }
}
""",
    "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java": """package com.sample.orders.filter;

import com.sample.orders.service.OrderServiceImpl;
import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import java.io.IOException;

/** Servlet filter: rejects ineligible orders before they reach the handler. */
public class SampleOrderFilter implements Filter {

    private final OrderServiceImpl service = new OrderServiceImpl();

    @Override
    public void init(FilterConfig filterConfig) {
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        String orderType = request.getParameter("orderType");
        if (!service.isEligible(orderType, request.getParameter("customerSegment"))) {
            throw new ServletException("order filtered: ineligible orderType");
        }
        chain.doFilter(request, response);
    }

    @Override
    public void destroy() {
    }
}
""",
    "service/src/main/java/com/sample/orders/handler/OrderServiceHandler.java": """package com.sample.orders.handler;

/** Service handler: entry point for order qualification requests. */
public class OrderServiceHandler {

    public String handle(String orderId) {
        if (orderId == null || orderId.isEmpty()) {
            throw new IllegalArgumentException("orderId is required");
        }
        return "QUALIFIED:" + orderId;
    }
}
""",
    "service/src/main/java/com/sample/orders/helper/OrderQualifyHelper.java": """package com.sample.orders.helper;

import com.sample.orders.handler.OrderServiceHandler;

/** Helper: validates input, then delegates to the order service handler. */
public class OrderQualifyHelper {

    private final OrderServiceHandler handler = new OrderServiceHandler();

    public String qualify(String orderId, boolean preChecked) {
        if (!preChecked) {
            return handler.handle(orderId);
        }
        return "PRECHECKED:" + orderId;
    }
}
""",
    "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java": """package com.sample.orders.proxy;

import com.sample.orders.handler.OrderServiceHandler;

/** REST proxy: remote facade over the order service handler. */
public class OrderServiceRestProxy {

    private final OrderServiceHandler handler = new OrderServiceHandler();

    public String submit(String orderId) {
        return handler.handle(orderId);
    }
}
""",
    "service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java": """package com.sample.orders.legacy;

/** Legacy translator: maps legacy order codes onto canonical types. */
public class LegacyCodeMapper {

    public String normalize(String orderType) {
        if (orderType == null) {
            return "STANDARD";
        }
        switch (orderType) {
            case "LEGACY-BULK":
                return "BULK";
            case "LEGACY-RES":
                return "RESIDENTIAL";
            default:
                return orderType;
        }
    }
}
""",
    "commons/src/main/java/com/sample/commons/SamplePreconditions.java": """package com.sample.commons;

/** Shared library: null/empty guards used across modules. */
public final class SamplePreconditions {

    private SamplePreconditions() {
    }

    public static String requireText(String value, String name) {
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }
}
""",
}

ORDER_SERVICE_V2 = """package com.sample.orders.service;

import com.sample.commons.SamplePreconditions;
import com.sample.orders.legacy.LegacyCodeMapper;

/** Spring service implementation: determines order eligibility. */
public class OrderServiceImpl {

    private final LegacyCodeMapper mapper = new LegacyCodeMapper();

    public boolean isEligible(String orderType, String customerSegment) {
        SamplePreconditions.requireText(customerSegment, "customerSegment");
        // Null orderType falls back to the STANDARD default so legacy
        // callers without a type still filter deterministically.
        String normalized = mapper.normalize(orderType);
        if ("STANDARD".equals(normalized) && "BULK".equals(customerSegment)) {
            return false;
        }
        return true;
    }
}
"""

ORDER_SERVICE_V3 = """package com.sample.orders.service;

import com.sample.commons.SamplePreconditions;
import com.sample.orders.legacy.LegacyCodeMapper;

/** Spring service implementation: determines order eligibility. */
public class OrderServiceImpl {

    private final LegacyCodeMapper mapper = new LegacyCodeMapper();

    public boolean isEligible(String orderType, String customerSegment) {
        SamplePreconditions.requireText(customerSegment, "customerSegment");
        // Null orderType falls back to the STANDARD default so legacy
        // callers without a type still filter deterministically. RESIDENTIAL
        // callers are always eligible regardless of segment.
        String normalized = mapper.normalize(orderType);
        if ("RESIDENTIAL".equals(normalized)) {
            return true;
        }
        if ("STANDARD".equals(normalized) && "BULK".equals(customerSegment)) {
            return false;
        }
        return true;
    }
}
"""

FILTER_V2 = """package com.sample.orders.filter;

import com.sample.orders.service.OrderServiceImpl;
import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import java.io.IOException;

/** Servlet filter: rejects ineligible orders before they reach the handler. */
public class SampleOrderFilter implements Filter {

    private static final int ELIGIBILITY_WINDOW_SECONDS = 300;

    private final OrderServiceImpl service = new OrderServiceImpl();

    @Override
    public void init(FilterConfig filterConfig) {
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        String orderType = request.getParameter("orderType");
        if (!service.isEligible(orderType, request.getParameter("customerSegment"))) {
            throw new ServletException("order filtered: ineligible orderType");
        }
        chain.doFilter(request, response);
    }

    @Override
    public void destroy() {
    }
}
"""


def run_git(repo: str, *args: str, env: dict) -> None:
    subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, env=env, check=True,
                   capture_output=True, text=True)


LIBS_FILES_V1 = {
    "settings.gradle": "rootProject.name = 'shared-libs'\n",
    "build.gradle": "plugins { id 'java' }\n",
    "src/main/java/com/sample/shared/SharedStrings.java": """package com.sample.shared;

/** Shared library: string helpers used across sample services. */
public final class SharedStrings {

    private SharedStrings() {
    }

    public static boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }

    public static String defaultIfBlank(String value, String fallback) {
        return isBlank(value) ? fallback : value;
    }
}
""",
}


def _build_repo(root, files_v1, initial_msg, extra_steps, env) -> str:
    """Write files, init git, apply extra ({rel: content}, message) steps."""
    from pathlib import Path as _Path

    root = _Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files_v1.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    run_git(str(root), "init", "-q", "-b", "main", ".", env=env)
    run_git(str(root), "add", "-A", env=env)
    run_git(str(root), "commit", "-q", "-m", initial_msg, env=env)
    for changes, message in extra_steps:
        for rel, content in changes.items():
            (root / rel).write_text(content, encoding="utf-8")
        run_git(str(root), "add", "-A", env=env)
        run_git(str(root), "commit", "-q", "-m", message, env=env)
    log = subprocess.run(["git", "log", "--oneline"], cwd=str(root), env=env,
                         check=True, capture_output=True, text=True).stdout
    return log


def main() -> int:
    from pathlib import Path

    base = Path(__file__).resolve().parents[1]
    targets = [
        (base / REPO, FILES_V1, "Initial Spring sample checkout", [
            ({"service/src/main/java/com/sample/orders/service/OrderServiceImpl.java":
              ORDER_SERVICE_V2}, "Guard null orderType with STANDARD default"),
            ({"service/src/main/java/com/sample/orders/service/OrderServiceImpl.java":
              ORDER_SERVICE_V3,
              "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java":
              FILTER_V2}, "Tighten filter eligibility window"),
        ]),
    ]
    targets.append((
        base / "data/code-repos/samplecorp/shared-libs",
        LIBS_FILES_V1,
        "Initial shared library checkout",
        [],
    ))
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    with tempfile.TemporaryDirectory(prefix="iap-sample-git-home-") as fake_home:
        env["HOME"] = fake_home
        code = 0
        for root, files_v1, initial_msg, extra in targets:
            if (root / ".git").exists():
                print(f"skip: {root} already holds a git repo.")
                continue
            log = _build_repo(root, files_v1, initial_msg, extra, env)
            print(f"built {root}:\n{log}")
    return code


if __name__ == "__main__":
    sys.exit(main())
