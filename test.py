import json
import os



def searchForDeletedMessage(id):
        with open(os.path.join("./temp", "chat-logs.json"), "r", encoding="utf-8") as f:
            content = json.loads(f.read())

            for day in content:
                  for msg in content.get(day):
                        msg_id = msg.get("id")

                        if msg_id == id:
                              return msg

            print("Could not find message with that id.")
            return {"status": "Failure"}
            #item = json.loads(l)
            #print(item)
            #if item.get("id") == id:
            #    print(item)
        pass

print(searchForDeletedMessage(1))