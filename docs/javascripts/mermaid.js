document$.subscribe(() => {
    mermaid.run({
        querySelector: ".mermaid",
    });
});
