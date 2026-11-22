package io.github.muzzikon.tagcleaner;

import android.graphics.Bitmap;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.StringJoiner;
import java.util.function.BooleanSupplier;

final class CoverSearch {
    private static final String MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording";
    private static final String ARCHIVE = "https://coverartarchive.org/release/";
    private static final String USER_AGENT = "TagCleanerAndroid/1.0 (https://github.com/Muzzikon/TagCleaner)";

    record Edition(String id, String name, String date, String country, boolean sameAlbum, boolean official, long distance) {}
    record Candidate(Edition edition, Bitmap preview) {}

    private long lastMusicBrainzRequest = -1000;

    List<Candidate> find(String artist, String title, String album, double duration, BooleanSupplier cancelled)
            throws Exception {
        long millis = Math.round(1000 * duration);
        long tolerance = tolerance(millis);
        var durations = new StringJoiner(" OR ", " AND (", ")");
        for (long bucket = Math.max(0, millis - tolerance) / 2000; bucket <= (millis + tolerance) / 2000; bucket++) {
            durations.add("qdur:" + bucket);
        }
        String query = "recording:" + quote(title) + " AND artist:" + quote(artist) + durations;
        String url = MUSICBRAINZ + "?fmt=json&limit=50&query=" + URLEncoder.encode(query, "UTF-8").replace("+", "%20");
        byte[] body = searchRecording(url, cancelled);
        if (cancelled.getAsBoolean()) return new ArrayList<>();
        if (body == null) throw new IOException("MusicBrainz не вернул результаты.");
        List<Edition> editions = editions(new JSONObject(new String(body, StandardCharsets.UTF_8)),
                artist, title, album, millis);
        var results = new ArrayList<Candidate>();
        int checked = 0;
        IOException networkError = null;
        for (Edition edition : editions) {
            if (results.size() == 4 || checked++ == 8 || cancelled.getAsBoolean()) break;
            try {
                byte[] bytes = fetch(ARCHIVE + edition.id() + "/front-250", 2 * 1024 * 1024);
                Bitmap bitmap = bytes == null ? null : Documents.preview(bytes, 250);
                if (bitmap != null) results.add(new Candidate(edition, bitmap));
            } catch (IOException error) {
                networkError = error;
            }
        }
        if (results.isEmpty() && networkError != null && !cancelled.getAsBoolean()) throw networkError;
        return results;
    }

    private byte[] searchRecording(String url, BooleanSupplier cancelled) throws Exception {
        for (int attempt = 0; ; attempt++) {
            long gap = attempt == 0 ? 1000 : 1500L * attempt;
            long wait = gap - (SystemClock.elapsedRealtime() - lastMusicBrainzRequest);
            if (wait > 0) Thread.sleep(wait);
            if (cancelled.getAsBoolean()) return null;
            lastMusicBrainzRequest = SystemClock.elapsedRealtime();
            try {
                return fetch(url, 2 * 1024 * 1024);
            } catch (HttpStatus error) {
                if (attempt >= 2 || (error.status != 429 && error.status != 503)) throw error;
            }
        }
    }

    byte[] artwork(Candidate candidate) throws IOException {
        byte[] content = fetch(ARCHIVE + candidate.edition().id() + "/front-500", Documents.IMAGE_LIMIT);
        if (content == null) throw new IOException("Обложка больше недоступна в каталоге.");
        return content;
    }

    static List<Edition> editions(JSONObject data, String artist, String title, String album, long duration)
            throws Exception {
        JSONArray recordings = data.getJSONArray("recordings");
        var matches = new LinkedHashMap<String, Edition>();
        for (int index = 0; index < recordings.length(); index++) {
            JSONObject recording = recordings.getJSONObject(index);
            long length = recording.optLong("length", 0);
            long delta = Math.abs(length - duration);
            if (length <= 0 || duration <= 0 || delta > tolerance(duration)
                    || recording.optBoolean("video")
                    || !normalized(title).equals(normalized(recording.optString("title")))
                    || !normalized(artist).equals(normalized(artistCredit(recording.optJSONArray("artist-credit"))))) {
                continue;
            }
            JSONArray releases = recording.optJSONArray("releases");
            if (releases == null) continue;
            for (int number = 0; number < releases.length(); number++) {
                JSONObject release = releases.getJSONObject(number);
                String id = release.optString("id");
                String name = release.optString("title");
                if (!id.matches("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}") || name.trim().isEmpty()) continue;
                matches.putIfAbsent(id, new Edition(id, name, release.optString("date"),
                        release.optString("country"), normalized(name).equals(normalized(album)),
                        "Official".equals(release.optString("status")), delta));
            }
        }
        var result = new ArrayList<>(matches.values());
        result.sort(Comparator.comparing(Edition::sameAlbum).thenComparing(Edition::official).reversed()
                .thenComparingLong(Edition::distance));
        return result;
    }

    private static String artistCredit(JSONArray credit) throws Exception {
        if (credit == null) return "";
        var result = new StringBuilder();
        for (int i = 0; i < credit.length(); i++) {
            JSONObject part = credit.getJSONObject(i);
            result.append(part.optString("name")).append(part.optString("joinphrase"));
        }
        return result.toString();
    }

    private static long tolerance(long duration) {
        return Math.max(2000, Math.min(4000, duration / 100));
    }

    private static String normalized(String value) {
        return Normalizer.normalize(value, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT)
                .replaceAll("[‘’]", "'").replaceAll("[–—]", "-").replaceAll("\\s+", " ").trim();
    }

    private static String quote(String value) {
        return '"' + value.replace("\\", "\\\\").replace("\"", "\\\"") + '"';
    }

    private static byte[] fetch(String address, int limit) throws IOException {
        URL url = new URL(address);
        for (int redirect = 0; redirect <= 4; redirect++) {
            String host = url.getHost().toLowerCase(Locale.ROOT);
            if (!"https".equals(url.getProtocol()) || url.getUserInfo() != null || url.getPort() != -1
                    || !(host.equals("musicbrainz.org") || host.equals("coverartarchive.org")
                    || host.equals("archive.org") || host.endsWith(".archive.org"))) {
                throw new IOException("Каталог перенаправил запрос на неизвестный сервер.");
            }
            var connection = (HttpURLConnection) url.openConnection();
            connection.setRequestProperty("User-Agent", USER_AGENT);
            connection.setConnectTimeout(6000);
            connection.setReadTimeout(6000);
            connection.setInstanceFollowRedirects(false);
            try {
                int status = connection.getResponseCode();
                if (status == 404) return null;
                if (status >= 300 && status < 400) {
                    String target = connection.getHeaderField("Location");
                    if (target == null) throw new IOException("Сервер не вернул адрес перенаправления.");
                    url = new URL(url, target.replaceFirst("^http://", "https://"));
                } else if (status == 200) {
                    try (var stream = connection.getInputStream()) {
                        return Documents.readLimited(stream, limit);
                    }
                } else {
                    throw new HttpStatus(status);
                }
            } finally {
                connection.disconnect();
            }
        }
        throw new IOException("Каталог вернул слишком много перенаправлений.");
    }

    private static final class HttpStatus extends IOException {
        final int status;

        HttpStatus(int status) {
            super("Каталог временно недоступен (HTTP " + status + ").");
            this.status = status;
        }
    }
}
