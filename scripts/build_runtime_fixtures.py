#!/usr/bin/env python3
"""Build long-lived HTTP applications for Beyla runtime detection tests."""

from __future__ import annotations

import argparse
from pathlib import Path


NAMESPACE = "stack-validation-runtime"
NODE_LABEL = "stack-validation-real"

RUNTIMES = {
    "go": ("golang:1.24-alpine", "go", '''package main
import ("fmt"; "net/http")
func main() { http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "go") }); http.ListenAndServe(":8080", nil) }
''', ["sh", "-c", "go run /app/server.go"]),
    "java": ("eclipse-temurin:21-jdk-alpine", "java", '''import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
public class Server { public static void main(String[] args) throws Exception { var s=HttpServer.create(new InetSocketAddress(8080), 0); s.createContext("/", e -> { byte[] b="java\\n".getBytes(); e.sendResponseHeaders(200,b.length); e.getResponseBody().write(b); e.close(); }); s.start(); } }
''', ["sh", "-c", "javac /app/Server.java && java -cp /app Server"]),
    "nodejs": ("node:22-alpine", "nodejs", '''const http = require("http"); http.createServer((_, response) => { response.end("nodejs\\n"); }).listen(8080);
''', ["node", "/app/server.js"]),
    "python": ("python:3.12-alpine", "python", '''from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"python\\n")
    def log_message(self, *_): pass
HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
''', ["python", "/app/server.py"]),
    "dotnet": ("mcr.microsoft.com/dotnet/sdk:8.0-alpine", "dotnet", '''<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>
''', ["sh", "-c", "dotnet run --project /app/Server.csproj --urls http://0.0.0.0:8080"]),
}


def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def build() -> str:
    docs = [f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n"]
    filenames = {"go": "server.go", "java": "Server.java", "nodejs": "server.js", "python": "server.py", "dotnet": "Server.csproj"}
    for runtime, (image, _, source, command) in RUNTIMES.items():
        config_name = f"beyla-runtime-{runtime}-source"
        config_data = [f"  {filenames[runtime]}: |", *[f"    {line}" for line in source.rstrip().splitlines()]]
        if runtime == "dotnet":
            config_data.extend([
                "  Program.cs: |",
                "    var builder = WebApplication.CreateBuilder(args);",
                "    var app = builder.Build();",
                '    app.MapGet("/", () => "dotnet");',
                "    app.Run();",
            ])
        docs.append("\n".join(["apiVersion: v1", "kind: ConfigMap", "metadata:", f"  name: {config_name}", f"  namespace: {NAMESPACE}", "data:", *config_data]) + "\n")
        labels = ["app.kubernetes.io/name: beyla-runtime-fixture", f"kubex.ai/runtime: {runtime}", "kubex.ai/beyla-test: \"true\""]
        docs.append("\n".join([
            "apiVersion: apps/v1", "kind: Deployment", "metadata:", f"  name: beyla-runtime-{runtime}", f"  namespace: {NAMESPACE}", "  labels:", *[f"    {x}" for x in labels], "spec:", "  replicas: 1", "  selector:", "    matchLabels:", f"      kubex.ai/runtime: {runtime}", "  template:", "    metadata:", "      labels:", *[f"        {x}" for x in labels], "    spec:", "      nodeSelector:", f"        {NODE_LABEL}: \"true\"", "      containers:", "      - name: app", f"        image: {image}", "        imagePullPolicy: IfNotPresent", f"        command: {json_array(command)}", "        ports:", "        - name: http", "          containerPort: 8080", "          protocol: TCP", "        volumeMounts:", "        - name: source", "          mountPath: /app", "      volumes:", "      - name: source", "        configMap:", f"          name: {config_name}",
        ]) + "\n")
        docs.append("\n".join(["apiVersion: v1", "kind: Service", "metadata:", f"  name: beyla-runtime-{runtime}", f"  namespace: {NAMESPACE}", "  labels:", *[f"    {x}" for x in labels], "spec:", "  selector:", f"    kubex.ai/runtime: {runtime}", "  ports:", "  - name: http", "    port: 8080", "    targetPort: http"]) + "\n")
    return "---\n".join(docs) + "\n"


def json_array(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_yaml_quote(value)}"' for value in values) + "]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
