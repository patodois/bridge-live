/*
 * JfrSummary: a streaming summary of one Java Flight Recorder file, for bridge-live's minecraft-link profiling.
 *
 * Run it with a JDK 17 or newer and nothing else:   java tools/JfrSummary.java [--package a.b.c]... [--top N] recording.jfr
 *
 * The idea (read the recording as a stream, keep a handful of event types, never export the recording's command line,
 * environment or system properties) comes from Horizon's tools/JfrSummary.java and docs/profiling.md
 * (MIT licence).
 * The code below is written for this repository and shares none of Horizon's; the credit is for the idea.
 *
 * What it prints (one JSON object on stdout):
 *   cpu         per thread: CPU seconds from jdk.ThreadCPULoad (user + system, scaled by the machine's hardware threads),
 *   allocation  per thread: bytes allocated between the first and the last jdk.ThreadAllocationStatistics counter,
 *   gc          collections, pause total and longest, heap used at the end of a collection,
 *   mod         the busiest methods of the given packages (our mod), from execution and allocation samples.
 *
 * What it never prints: the JVM arguments, the program's command line, environment variables, system properties, file or
 * socket events, and any string that looks like a user path (a final pass replaces the home folder and /Users/<name>,
 * /home/<name>, C:\Users\<name> with "~"). Raw recordings still contain all of that: keep them private and delete them.
 */

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;
import jdk.jfr.consumer.RecordedEvent;
import jdk.jfr.consumer.RecordedFrame;
import jdk.jfr.consumer.RecordedStackTrace;
import jdk.jfr.consumer.RecordedThread;
import jdk.jfr.consumer.RecordingFile;

public final class JfrSummary {

    // ------------------------------------------------------------------------------------------ per-thread state

    /** CPU load samples of one thread: each sample is a fraction of the whole machine over the time since the last one. */
    private static final class CpuThread {
        String name;
        final List<Instant> times = new ArrayList<>();
        final List<Double> user = new ArrayList<>();
        final List<Double> system = new ArrayList<>();
    }

    /** First and last value of a thread's cumulative allocation counter. */
    private static final class AllocThread {
        String name;
        Instant firstAt, lastAt;
        long first, last;
        int seen;

        void see(Instant at, long bytes) {
            seen++;
            if (firstAt == null || at.isBefore(firstAt)) {
                firstAt = at;
                first = bytes;
            }
            if (lastAt == null || at.isAfter(lastAt)) {
                lastAt = at;
                last = bytes;
            }
        }
    }

    /** Sampled stacks of the mod's packages. */
    private static final class ModSamples {
        long total, inMod, noStack;
        final Map<String, Long> self = new HashMap<>();        // the sampled frame itself is a mod method
        final Map<String, Long> innermost = new HashMap<>();   // the mod method closest to the sampled frame
        final Map<String, Long> inclusive = new HashMap<>();   // the mod method anywhere in the stack, once per sample
    }

    private final List<String> packages;
    private final Map<Long, CpuThread> cpu = new HashMap<>();
    private final Map<Long, AllocThread> alloc = new HashMap<>();
    private final ModSamples execution = new ModSamples();
    private final ModSamples allocation = new ModSamples();
    private long events;
    private Instant firstEvent, lastEvent;
    private int hwThreads;
    private final Map<String, String> jvm = new LinkedHashMap<>();
    private long gcCycles;
    private double gcPauseMs, gcLongestMs;
    private final Map<String, long[]> gcByName = new TreeMap<>();  // name -> {count}
    private long heapPeak;

    private JfrSummary(List<String> packages) {
        this.packages = packages;
    }

    // ------------------------------------------------------------------------------------------ reading

    private void read(Path file) throws Exception {
        try (RecordingFile rf = new RecordingFile(file)) {
            while (rf.hasMoreEvents()) {
                RecordedEvent e = rf.readEvent();
                events++;
                Instant start = e.getStartTime();
                if (firstEvent == null || start.isBefore(firstEvent)) firstEvent = start;
                if (lastEvent == null || e.getEndTime().isAfter(lastEvent)) lastEvent = e.getEndTime();
                switch (e.getEventType().getName()) {
                    case "jdk.ThreadCPULoad" -> {
                        RecordedThread t = e.getThread("eventThread");
                        if (t == null) break;
                        CpuThread c = cpu.computeIfAbsent(key(t), k -> new CpuThread());
                        c.name = name(t);
                        c.times.add(start);
                        c.user.add((double) e.getFloat("user"));
                        c.system.add((double) e.getFloat("system"));
                    }
                    case "jdk.ThreadAllocationStatistics" -> {
                        RecordedThread t = e.getThread("thread");
                        if (t == null) break;
                        AllocThread a = alloc.computeIfAbsent(key(t), k -> new AllocThread());
                        a.name = name(t);
                        a.see(start, e.getLong("allocated"));
                    }
                    case "jdk.ExecutionSample", "jdk.NativeMethodSample" -> sample(execution, e.getStackTrace());
                    case "jdk.ObjectAllocationSample" -> sample(allocation, e.getStackTrace());
                    case "jdk.GarbageCollection" -> {
                        gcCycles++;
                        gcPauseMs += ms(e.getDuration("sumOfPauses"));
                        gcLongestMs = Math.max(gcLongestMs, ms(e.getDuration("longestPause")));
                        gcByName.computeIfAbsent(e.getString("name"), k -> new long[1])[0]++;
                    }
                    case "jdk.GCHeapSummary" -> heapPeak = Math.max(heapPeak, e.getLong("heapUsed"));
                    case "jdk.CPUInformation" -> hwThreads = Math.max(hwThreads, e.getInt("hwThreads"));
                    case "jdk.JVMInformation" -> {
                        // Two fields only. This event also carries the JVM arguments and the program's command line: never read here.
                        jvm.put("name", e.getString("jvmName"));
                        jvm.put("version", e.getString("jvmVersion"));
                    }
                    default -> { }
                }
            }
        }
    }

    private static long key(RecordedThread t) {
        return t.getJavaThreadId() > 0 ? t.getJavaThreadId() : -t.getOSThreadId();
    }

    private static String name(RecordedThread t) {
        String n = t.getJavaName() != null ? t.getJavaName() : t.getOSName();
        return n == null || n.isEmpty() ? "(unnamed)" : n;
    }

    private static double ms(Duration d) {
        return d == null ? 0 : d.toNanos() / 1e6;
    }

    private boolean isMod(String className) {
        for (String p : packages) {
            if (className.equals(p) || className.startsWith(p + ".")) return true;
        }
        return false;
    }

    /** Hidden classes of lambdas carry a per-run address (Foo$$Lambda.0x000003fc02907d78): drop it so runs line up. */
    private static final Pattern HIDDEN_ADDRESS = Pattern.compile("\\.0x[0-9a-fA-F]+");

    private static String label(RecordedFrame f) {
        return HIDDEN_ADDRESS.matcher(f.getMethod().getType().getName()).replaceAll("") + "." + f.getMethod().getName();
    }

    private void sample(ModSamples s, RecordedStackTrace st) {
        s.total++;
        if (st == null || st.getFrames().isEmpty()) {
            s.noStack++;
            return;
        }
        List<RecordedFrame> frames = st.getFrames();   // index 0 is the sampled (innermost) frame
        Set<String> seen = new HashSet<>();
        String innermost = null;
        for (RecordedFrame f : frames) {
            if (!isMod(f.getMethod().getType().getName())) continue;
            String l = label(f);
            if (innermost == null) innermost = l;
            if (seen.add(l)) s.inclusive.merge(l, 1L, Long::sum);
        }
        if (innermost == null) return;
        s.inMod++;
        s.innermost.merge(innermost, 1L, Long::sum);
        if (isMod(frames.get(0).getMethod().getType().getName())) s.self.merge(label(frames.get(0)), 1L, Long::sum);
    }

    // ------------------------------------------------------------------------------------------ result

    private Map<String, Object> cpuResult(int top) {
        int cores = Math.max(1, hwThreads);
        // The sampling period: the most common gap between two samples of one thread (the first sample of a thread has no gap behind it).
        Map<Long, Integer> gaps = new HashMap<>();
        for (CpuThread c : cpu.values()) {
            for (int i = 1; i < c.times.size(); i++) {
                gaps.merge(Math.round(Duration.between(c.times.get(i - 1), c.times.get(i)).toMillis() / 100.0) * 100, 1, Integer::sum);
            }
        }
        long period = gaps.entrySet().stream().max(Map.Entry.comparingByValue()).map(Map.Entry::getKey).orElse(1000L);
        List<Map<String, Object>> rows = new ArrayList<>();
        double total = 0;
        for (CpuThread c : cpu.values()) {
            double user = 0, sys = 0, peak = 0;
            for (int i = 0; i < c.times.size(); i++) {
                long gap = i == 0 ? period : Duration.between(c.times.get(i - 1), c.times.get(i)).toMillis();
                double secs = gap / 1000.0 * cores;
                user += c.user.get(i) * secs;
                sys += c.system.get(i) * secs;
                peak = Math.max(peak, (c.user.get(i) + c.system.get(i)) * cores);
            }
            total += user + sys;
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("thread", c.name);
            row.put("cpu_seconds", round(user + sys, 3));
            row.put("user_seconds", round(user, 3));
            row.put("system_seconds", round(sys, 3));
            row.put("peak_cores", round(peak, 2));
            row.put("samples", c.times.size());
            rows.add(row);
        }
        rows.sort(Comparator.<Map<String, Object>>comparingDouble(r -> -(Double) r.get("cpu_seconds")).thenComparing(r -> (String) r.get("thread")));
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("hw_threads", hwThreads);
        out.put("sample_period_ms", period);
        out.put("threads_seen", rows.size());
        out.put("total_cpu_seconds", round(total, 3));
        out.put("top_threads", rows.subList(0, Math.min(top, rows.size())));
        return out;
    }

    private Map<String, Object> allocResult(int top) {
        List<Map<String, Object>> rows = new ArrayList<>();
        long total = 0;
        int singleReading = 0;
        for (AllocThread a : alloc.values()) {
            if (a.seen < 2 || a.last < a.first) {   // one counter reading says nothing about a rate
                singleReading++;
                continue;
            }
            long bytes = a.last - a.first;
            double secs = Duration.between(a.firstAt, a.lastAt).toNanos() / 1e9;
            total += bytes;
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("thread", a.name);
            row.put("bytes", bytes);
            row.put("observed_seconds", round(secs, 2));
            row.put("bytes_per_second", secs > 0 ? Math.round(bytes / secs) : null);
            rows.add(row);
        }
        rows.sort(Comparator.<Map<String, Object>>comparingLong(r -> -(Long) r.get("bytes")).thenComparing(r -> (String) r.get("thread")));
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("total_bytes_observed", total);
        out.put("threads_with_a_rate", rows.size());
        out.put("threads_with_one_reading_left_out", singleReading);
        out.put("top_threads", rows.subList(0, Math.min(top, rows.size())));
        return out;
    }

    private Map<String, Object> gcResult() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("collections", gcCycles);
        Map<String, Object> by = new LinkedHashMap<>();
        gcByName.forEach((k, v) -> by.put(k, v[0]));
        out.put("by_collector", by);
        out.put("pause_total_ms", round(gcPauseMs, 2));
        out.put("pause_longest_ms", round(gcLongestMs, 2));
        out.put("heap_used_peak_bytes", heapPeak == 0 ? null : heapPeak);
        return out;
    }

    private static List<Map<String, Object>> top(Map<String, Long> counts, int n) {
        List<Map<String, Object>> rows = new ArrayList<>();
        counts.entrySet().stream()
            .sorted(Map.Entry.<String, Long>comparingByValue().reversed().thenComparing(Map.Entry.comparingByKey()))
            .limit(n)
            .forEach(e -> {
                Map<String, Object> r = new LinkedHashMap<>();
                r.put("method", e.getKey());
                r.put("samples", e.getValue());
                rows.add(r);
            });
        return rows;
    }

    private Map<String, Object> modResult(ModSamples s, int n) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("samples", s.total);
        out.put("samples_without_stack", s.noStack);
        out.put("samples_with_a_mod_frame", s.inMod);
        out.put("top_self", top(s.self, n));
        out.put("top_innermost_mod_frame", top(s.innermost, n));
        out.put("top_inclusive", top(s.inclusive, n));
        return out;
    }

    private Map<String, Object> result(int top) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("schema", 1);
        out.put("jvm", new LinkedHashMap<>(jvm));
        out.put("events_read", events);
        out.put("event_span_seconds", firstEvent == null ? 0 : round(Duration.between(firstEvent, lastEvent).toNanos() / 1e9, 2));
        out.put("cpu", cpuResult(top));
        out.put("allocation", allocResult(top));
        out.put("gc", gcResult());
        Map<String, Object> mod = new LinkedHashMap<>();
        mod.put("packages", packages);
        mod.put("execution", modResult(execution, top));
        mod.put("allocation_samples", modResult(allocation, top));
        out.put("mod", mod);
        out.put("notes", List.of(
            "CPU seconds are sums of jdk.ThreadCPULoad fractions times the time since the previous sample times the machine's hardware threads; a thread's life before its first sample is counted as one sampling period. Approximate.",
            "Allocation bytes are the difference between the first and the last cumulative counter of each thread; threads with a single reading are left out.",
            "Execution and allocation samples locate where a thread was, they are not exact CPU time or allocated bytes. 'inclusive' counts a method once per sample, 'innermost' the mod method closest to the sampled frame.",
            "GC pauses are the sums reported by jdk.GarbageCollection; concurrent phases are not pauses.",
            "Profiling adds overhead: a run recorded with JFR is not a baseline."));
        return out;
    }

    private static double round(double v, int places) {
        double f = Math.pow(10, places);
        return Math.round(v * f) / f;
    }

    // ------------------------------------------------------------------------------------------ output

    private static final Pattern USER_PATH = Pattern.compile(
        "(/Users/[^/\\s\"']+|/home/[^/\\s\"']+|[A-Za-z]:\\\\Users\\\\[^\\\\\\s\"']+)");

    /** No string leaves with a user path in it. */
    static String scrub(String s) {
        String out = s;
        String home = System.getProperty("user.home");
        if (home != null && home.length() > 1) out = out.replace(home, "~");
        return USER_PATH.matcher(out).replaceAll("~");
    }

    static void json(Object v, StringBuilder sb) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            sb.append('"');
            String t = scrub(s);
            for (int i = 0; i < t.length(); i++) {
                char c = t.charAt(i);
                switch (c) {
                    case '"' -> sb.append("\\\"");
                    case '\\' -> sb.append("\\\\");
                    case '\n' -> sb.append("\\n");
                    case '\r' -> sb.append("\\r");
                    case '\t' -> sb.append("\\t");
                    default -> {
                        if (c < 0x20) sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                        else sb.append(c);
                    }
                }
            }
            sb.append('"');
        } else if (v instanceof Double d) {
            sb.append(d.isNaN() || d.isInfinite() ? "null" : String.valueOf(d));
        } else if (v instanceof Number || v instanceof Boolean) {
            sb.append(v);
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                json(String.valueOf(e.getKey()), sb);
                sb.append(':');
                json(e.getValue(), sb);
            }
            sb.append('}');
        } else if (v instanceof List<?> l) {
            sb.append('[');
            for (int i = 0; i < l.size(); i++) {
                if (i > 0) sb.append(',');
                json(l.get(i), sb);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("not a JSON value: " + v.getClass());
        }
    }

    public static void main(String[] args) throws Exception {
        List<String> packages = new ArrayList<>();
        int top = 15;
        String file = null;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--package" -> packages.add(args[++i]);
                case "--top" -> top = Integer.parseInt(args[++i]);
                default -> {
                    if (file != null) throw new IllegalArgumentException("one recording only");
                    file = args[i];
                }
            }
        }
        if (file == null) {
            System.err.println("usage: java tools/JfrSummary.java [--package a.b.c]... [--top N] recording.jfr");
            System.exit(2);
        }
        JfrSummary s = new JfrSummary(packages);
        s.read(Path.of(file));
        StringBuilder sb = new StringBuilder();
        json(s.result(top), sb);
        System.out.println(sb);
    }
}
