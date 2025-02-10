#!/usr/bin/env nextflow

// This block is the interesting part - copy it into your own script to use Farmer with Nextflow.
workflow.onComplete {
    // Modify these variables to control the notification you receive.
    def user = workflow.userName
    def label = "Nextflow"
    def payload = [
        "job name: " + workflow.runName,
        "launch dir: " + workflow.launchDir,
        "script name: " + workflow.scriptName,
        "started: " + workflow.start,
        "completed: " + workflow.complete,
        "exit status: " + workflow.exitStatus,
        workflow.errorMessage ? "error message: " + workflow.errorMessage : null,
    ].minus(null).join("\n")
    // Leave this code as-is.
    URL url = new URL("http://farm22-cgi-01.internal.sanger.ac.uk:8234/job-complete");
    HttpURLConnection conn = (HttpURLConnection)url.openConnection();
    conn.setRequestMethod("POST");
    conn.setRequestProperty("Content-Type", "application/json; charset=UTF-8");
    conn.setDoOutput(true);
    conn.connect();
    OutputStream os = conn.getOutputStream();
    String json = groovy.json.JsonOutput.toJson([job_id: null, array_index: null, user_override: user, label: label, payload: payload]);
    os.write(json.getBytes("UTF-8"));
    os.close();
    conn.getResponseCode()
    conn.disconnect();
}

// The rest is just a very simple example workflow.
process sayHello {
    output:
        stdout

    script:
        """
        echo 'Hello World!'
        """
}
workflow {
    sayHello()
}
