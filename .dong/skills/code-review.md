# code-review

When reviewing code, check these aspects in order:

1. **Security**: SQL injection, path traversal, shell injection, hardcoded secrets
2. **Correctness**: Edge cases (empty, null, boundary), error handling, state mutations
3. **Performance**: Unnecessary loops, N+1 queries, memory allocation in hot paths
4. **Style**: Follow the project's existing patterns. Don't introduce a new style.
5. **Completeness**: Are there tests? Error messages are user-friendly? Logging is adequate?

Always point out the good parts too, not just problems.

Output format:
```
## Summary (1-2 sentences)

### 👍 Good
- ...

### ⚠️ Issues
- **Severity**: high/medium/low — description — suggested fix
```

