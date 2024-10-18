# How to test a LangGraph app locally

This guide assumes you have a LangGraph app correctly set up with a proper configuration file and a corresponding compiled graph, and that you have a proper LangChain API key.

Testing locally ensures that there are no errors or conflicts with Python dependencies and confirms that the configuration file is specified correctly.

## Setup

Install the proper packages:

```shell
pip install langgraph-cli
```

Ensure you have an API key, which you can create from the LangSmith UI (Settings > API Keys). This is required to authenticate that you have LangGraph Cloud access. After you have saved the key to a safe place, place the following line in your `.env` file:

```python
LANGCHAIN_API_KEY = *********
```

## Start the API server

Once you have downloaded the CLI, you can run the following command to start the API server for local testing:

```shell
langgraph up
```

This will start up the LangGraph API server locally. If this runs successfully, you should see something like:

```shell
Ready!
- API: http://localhost:8123
2024-06-26 19:20:41,056:INFO:uvicorn.access 127.0.0.1:44138 - "GET /ok HTTP/1.1" 200
```

### Interact with the server

We can now interact with the API server using the LangGraph SDK. First, we need to start our client, select our assistant (in this case a graph we called "agent", make sure to select the proper assistant you wish to test).

You can either initialize by passing authentication or by setting an environment variable.

#### Initialize with authentication

=== "Python"

    ```python
    from langgraph_sdk import get_client

    # only pass the url argument to get_client() if you changed the default port when calling langgraph up
    client = get_client(url=<DEPLOYMENT_URL>,api_key=<LANGCHAIN_API_KEY>)
    # Using the graph deployed with the name "agent"
    assistant_id = "agent"
    thread = await client.threads.create()
    ```

=== "Javascript"

    ```js
    import { Client } from "@langchain/langgraph-sdk";

    // only set the apiUrl if you changed the default port when calling langgraph up
    const client = new Client({ apiUrl: <DEPLOYMENT_URL>, apiKey: <LANGCHAIN_API_KEY> });
    // Using the graph deployed with the name "agent"
    const assistantId = "agent";
    const thread = await client.threads.create();
    ```

=== "CURL"

    ```bash
    curl --request POST \
      --url <DEPLOYMENT_URL>/threads \
      --header 'Content-Type: application/json'
      --header 'x-api-key: <LANGCHAIN_API_KEY>'
    ```
  

#### Initialize with environment variables

If you have a `LANGCHAIN_API_KEY` set in your environment, you do not need to explicitly pass authentication to the client

=== "Python"

    ```python
    from langgraph_sdk import get_client

    # only pass the url argument to get_client() if you changed the default port when calling langgraph up
    client = get_client()
    # Using the graph deployed with the name "agent"
    assistant_id = "agent"
    thread = await client.threads.create()
    ```

=== "Javascript"

    ```js
    import { Client } from "@langchain/langgraph-sdk";

    // only set the apiUrl if you changed the default port when calling langgraph up
    const client = new Client();
    // Using the graph deployed with the name "agent"
    const assistantId = "agent";
    const thread = await client.threads.create();
    ```

=== "CURL"

    ```bash
    curl --request POST \
      --url <DEPLOYMENT_URL>/threads \
      --header 'Content-Type: application/json'
    ```

Now we can invoke our graph to ensure it is working. Make sure to change the input to match the proper schema for your graph. 

=== "Python"

    ```python
    input = {"messages": [{"role": "user", "content": "what's the weather in sf"}]}
    async for chunk in client.runs.stream(
        thread["thread_id"],
        assistant_id,
        input=input,
        stream_mode="updates",
    ):
        print(f"Receiving new event of type: {chunk.event}...")
        print(chunk.data)
        print("\n\n")
    ```
=== "Javascript"

    ```js
    const input = { "messages": [{ "role": "user", "content": "what's the weather in sf"}] }

    const streamResponse = client.runs.stream(
      thread["thread_id"],
      assistantId,
      {
        input: input,
        streamMode: "updates",
      }
    );
    for await (const chunk of streamResponse) {
      console.log(`Receiving new event of type: ${chunk.event}...`);
      console.log(chunk.data);
      console.log("\n\n");
    }
    ```

  === "CURL"

    ```bash
    curl --request POST \
     --url <DEPLOYMENT_URL>/threads/<THREAD_ID>/runs/stream \
     --header 'Content-Type: application/json' \
     --data "{
       \"assistant_id\": \"agent\",
       \"input\": {\"messages\": [{\"role\": \"human\", \"content\": \"what's the weather in sf\"}]},
       \"stream_mode\": [
         \"events\"
       ]
     }" | \
     sed 's/\r$//' | \
     awk '
     /^event:/ {
         if (data_content != "") {
             print data_content "\n"
         }
         sub(/^event: /, "Receiving event of type: ", $0)
         printf "%s...\n", $0
         data_content = ""
     }
     /^data:/ {
         sub(/^data: /, "", $0)
         data_content = $0
     }
     END {
         if (data_content != "") {
             print data_content "\n"
         }
     }
     ' 
    ```

If your graph works correctly, you should see your graph output displayed in the console. Of course, there are many more ways you might need to test your graph, for a full list of commands you can send with the SDK, see the [Python](https://langchain-ai.github.io/langgraph/cloud/reference/sdk/python_sdk_ref/) and [JS/TS](https://langchain-ai.github.io/langgraph/cloud/reference/sdk/js_ts_sdk_ref/) references.