// Integration check using the same JDBC jar and credential helper as DBeaver.
// No passwords or tokens are written to arguments, logs, or the result file.
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.util.*;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.concurrent.TimeUnit;
import com.google.gson.*;

public class DBeaverJdbcCheck {
    static final Gson JSON = new GsonBuilder().setPrettyPrinting().create();
    static final List<String> checks = new ArrayList<>();
    static String stage = "configuration";
    static void check(boolean condition, String name) {
        if (!condition) throw new IllegalStateException(name);
        checks.add(name);
    }
    static String scalar(Connection c, String sql) throws SQLException {
        try (Statement s = c.createStatement(); ResultSet r = s.executeQuery(sql)) {
            if (!r.next()) throw new SQLException("Empty result");
            return r.getString(1);
        }
    }
    static Map<String,String> snapshot(Connection c) throws SQLException {
        Map<String,String> result = new TreeMap<>();
        List<String> tables = new ArrayList<>();
        try (Statement s = c.createStatement(); ResultSet r = s.executeQuery(
                "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname IN ('practice','reference','kafka_lab') ORDER BY 1")) {
            while (r.next()) tables.add(r.getString(1));
        }
        for (String table : tables) {
            if (!table.matches("[a-z_]+\\.[a-z_]+")) throw new SQLException("Unexpected table identifier");
            result.put(table, scalar(c, "SELECT md5(coalesce(string_agg(j, E'\\n' ORDER BY j),'')) FROM (SELECT row_to_json(t)::text AS j FROM "+table+" t) q"));
        }
        return result;
    }
    static Connection connect(Path root, String role) throws Exception {
        String powershell = System.getenv("SystemRoot")+"\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
        Process p = new ProcessBuilder(powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", root.resolve("scripts/lab/Get-DBeaverPassword.ps1").toString(), "-Role", role).start();
        if (!p.waitFor(12, TimeUnit.SECONDS)) { p.destroyForcibly(); throw new IllegalStateException("Credential helper timeout"); }
        String password = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim();
        if (p.exitValue()!=0 || password.isEmpty()) throw new IllegalStateException("Credential helper failed");
        Properties props = new Properties();
        props.setProperty("user", "aml_"+role);
        props.setProperty("password", password);
        props.setProperty("options", "-c timezone=UTC -c search_path=practice,reference,pg_catalog");
        props.setProperty("ApplicationName", "AML DBeaver qualification");
        props.setProperty("connectTimeout", "10"); props.setProperty("socketTimeout", "30");
        return DriverManager.getConnection("jdbc:postgresql://127.0.0.1:55432/aml_lab", props);
    }
    static void denied(Connection c, String sql, String name) throws SQLException {
        try (Statement s = c.createStatement()) {
            s.executeUpdate(sql);
            throw new IllegalStateException("Write unexpectedly allowed: "+name);
        } catch (SQLException e) {
            check("42501".equals(e.getSQLState()), name);
        } finally { if (!c.getAutoCommit()) c.rollback(); }
    }
    static HttpResponse<String> request(HttpClient http, String token, String method, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:8765"+path))
            .timeout(Duration.ofSeconds(10)).header("X-Lab-Key", token).header("Content-Type", "application/json");
        return http.send(b.method(method, body==null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body)).build(),
            HttpResponse.BodyHandlers.ofString());
    }
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]).toAbsolutePath().normalize();
        Path output = Path.of(args[1]).toAbsolutePath().normalize();
        if (!root.toString().equalsIgnoreCase("D:\\work\\aml-ai-agent-system-rebuild") || !output.startsWith(root.resolve("verification"))) throw new IllegalArgumentException("Invalid paths");
        if (Files.exists(output)) throw new IllegalStateException("Evidence already exists");
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("scope", "Real JDBC/HTTP/role/transaction integration; desktop GUI interactions require operator confirmation.");
        try {
            Class.forName("org.postgresql.Driver");
            JsonObject cfg = JsonParser.parseString(Files.readString(root.resolve("var/postgres/lab-connection.json")).replace("\uFEFF","")).getAsJsonObject();
            String token = cfg.get("api_token").getAsString();
            try (Connection student=connect(root,"student"); Connection reader=connect(root,"reader")) {
                student.setAutoCommit(false);
                reader.setAutoCommit(true);
                stage="identity";
                check("aml_student".equals(scalar(student,"SELECT current_user")), "student_auth");
                check("aml_reader".equals(scalar(reader,"SELECT current_user")), "reader_auth");
                check("aml_lab".equals(scalar(reader,"SELECT current_database()")), "database_identity");
                check("UTC".equals(scalar(student,"SHOW timezone")), "utc");
                check(scalar(student,"SHOW search_path").replace(" ","").equals("practice,reference,pg_catalog"), "search_path");
                check(!student.getAutoCommit() && reader.getAutoCommit(), "transaction_modes");
                Map<String,String> before = snapshot(reader);
                stage="permissions";
                denied(reader,"UPDATE practice.clients SET kyc_status=kyc_status WHERE false","reader_database_write_denied");
                denied(student,"UPDATE reference.clients SET kyc_status=kyc_status WHERE false","reference_write_denied");
                denied(student,"DELETE FROM kafka_lab.processed_events WHERE false","kafka_write_denied");
                check(Long.parseLong(scalar(reader,"SELECT count(*) FROM kafka_lab.processed_events"))>=0, "kafka_results_readable");
                check(Long.parseLong(scalar(reader,"SELECT count(*) FROM kafka_lab.analysis_requests"))>=0, "aml_projection_readable");
                stage="api_transaction";
                HttpClient http=HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(5)).proxy(new java.net.ProxySelector() {
                    public List<java.net.Proxy> select(URI uri) { return List.of(java.net.Proxy.NO_PROXY); }
                    public void connectFailed(URI uri, java.net.SocketAddress addr, java.io.IOException e) { }
                }).build();
                String incident=scalar(reader,"SELECT incident_id FROM practice.incidents ORDER BY incident_id LIMIT 1");
                String body="DBeaver qualification "+UUID.randomUUID();
                HttpResponse<String> created=request(http,token,"POST","/lab/notes",JSON.toJson(Map.of("incident_id",incident,"body",body)));
                check(created.statusCode()==201,"api_note_created");
                String note=JsonParser.parseString(created.body()).getAsJsonObject().get("id").getAsString();
                UUID.fromString(note);
                try {
                    check(body.equals(scalar(reader,"SELECT body FROM practice.case_notes WHERE id='"+note+"'::uuid")), "api_write_visible_in_jdbc");
                    try (PreparedStatement p=student.prepareStatement("UPDATE practice.case_notes SET body=? WHERE id=?::uuid")) {
                        p.setString(1,"uncommitted"); p.setString(2,note);
                        check(p.executeUpdate()==1,"student_update");
                    }
                    HttpResponse<String> pending=request(http,token,"GET","/lab/notes/"+note,null);
                    check(pending.statusCode()==200 && JsonParser.parseString(pending.body()).getAsJsonObject().get("body").getAsString().equals(body), "uncommitted_sql_hidden_from_api");
                    student.rollback();
                    check(body.equals(scalar(reader,"SELECT body FROM practice.case_notes WHERE id='"+note+"'::uuid")), "rollback_restores_note");
                    try (PreparedStatement p=student.prepareStatement("UPDATE practice.case_notes SET body=? WHERE id=?::uuid")) {
                        p.setString(1,"committed qualification"); p.setString(2,note); p.executeUpdate();
                    }
                    student.commit();
                    HttpResponse<String> saved=request(http,token,"GET","/lab/notes/"+note,null);
                    check(saved.statusCode()==200 && JsonParser.parseString(saved.body()).getAsJsonObject().get("body").getAsString().equals("committed qualification"), "committed_sql_visible_in_api");
                } finally {
                    student.rollback();
                    HttpResponse<String> deleted=request(http,token,"DELETE","/lab/notes/"+note+"?expected_revision=1",null);
                    check(deleted.statusCode()==204,"test_note_removed");
                }
                stage="preservation";
                check(before.equals(snapshot(reader)),"all_lab_table_rows_unchanged");
                result.put("table_count", before.size());
                result.put("driver_version", student.getMetaData().getDriverVersion());
                result.put("checks", checks); result.put("status","PASS");
            }
        } catch (Exception e) {
            result.put("status","FAIL"); result.put("stage",stage); result.put("checks",checks);
            result.put("error_type",e.getClass().getName());
            if(e instanceof SQLException) result.put("sqlstate",((SQLException)e).getSQLState());
        }
        Files.createDirectories(output.getParent());
        Files.writeString(output,JSON.toJson(result)+"\n",StandardCharsets.UTF_8);
        System.out.println(JSON.toJson(result));
        if (!"PASS".equals(result.get("status"))) System.exit(1);
    }
}
