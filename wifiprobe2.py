import logging
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

from scapy.all import sniff
import sys
import struct
import json
import psycopg2
import datetime
from io import open
import telegram
import manuf

MGMT_TYPE = 0x0
PROBE_SUBTYPE = 0x04

FMT_HEADER_80211 = "<HH6s6s6sH"
WLAN_MGMT_ELEMENT = "<BB"

TO_DS_BIT = 2**9
FROM_DS_BIT = 2**10

def encodeMac(s):
    return ''.join(( '%.2x' % ord(i) for i in s ))

class Handler(object):
    def __init__(self,conf):
        self.conf = conf
        self.conn = None
        
    def getDatabaseConnection(self):
    
        if self.conn == None:
            self.conn = psycopg2.connect(**conf)
            
        return self.conn
        
    def __call__(self,pkt):
        #If the packet is not a management packet ignore it
        if not pkt.type == MGMT_TYPE:
            return    
        bot = telegram.Bot(token='203410933:AAG6avZhedGbVsGZjgEa1x5u-DuNZ3BcjTE')

        #Extract the payload from the packet
        payload = buffer(str(pkt.payload))
        #Carve out just the header
        headerSize = struct.calcsize(FMT_HEADER_80211)
        header = payload[:headerSize]
        #unpack the header
        frameControl,dur,addr1,addr2,addr3,seq = struct.unpack(FMT_HEADER_80211,header)
        
        fromDs = (FROM_DS_BIT & frameControl) != 0
        toDs = (TO_DS_BIT & frameControl) != 0
        
        if fromDs and not toDs:
            srcAddr = addr3
        elif not  fromDs and not toDs:
            srcAddr = addr2
        elif not fromDs and toDs:
            srcAddr = addr2
        elif fromDs and toDs:
            return
        
        #Query the database to see the last time this station was seen
        conn = self.getDatabaseConnection()
        cur = conn.cursor()
        
        cur.execute('Select id,lastseen from station where mac = %s;',(encodeMac(srcAddr),))
        r = cur.fetchone()
        print(r)
        #If never seen, add the station to the database
        if r == None:
            #If seen, update the last seen time of the station 
            bot.getMe()
            updates = bot.getUpdates()
            print [u.message.text for u in updates]
            chat_id = bot.getUpdates()[-1].message.chat_id
            bot.sendMessage(chat_id=chat_id, text="ALERT! Wifi perimeter violation " + (r))
            def get_manuf(self, r):
                model = self.get_all(r).manuf
            
                cur.execute("""Insert into station(mac,model,firstSeen,lastSeen) VALUES(%s,%s,current_timestamp at time zone 'utc',current_timestamp at time zone 'utc') returning id;""",(encodeMac(srcAddr),model,))
                r = cur.fetchone()
                suid = r
        else:
            suid,lastSeen = r
            cur.execute('Update station set lastSeen = %s where id = %s',(datetime.datetime.now(),suid,))
        cur.close()
        conn.commit()
        
        #If the packet subtype is not probe or beacon ignore the rest of it
        isProbe = pkt.subtype == PROBE_SUBTYPE
        if not isProbe:
            return
        
        #Extract each tag from the payload
        tags = payload[headerSize:]
        
        ssid = None
        while len(tags) != 0:
            #Carve out and extract the id and length of the  tag
            tagHeader = tags[0:struct.calcsize(WLAN_MGMT_ELEMENT)]
            tagId,tagLength = struct.unpack(WLAN_MGMT_ELEMENT,tagHeader)
            tags = tags[struct.calcsize(WLAN_MGMT_ELEMENT):]

            #The tag id must be zero for SSID
            #The tag length must be greater than zero or it is a 
            #an anonymous probe
            #The tag length must be less than or equal to 32 or it is
            #not a valid SSID

            if tagId == 0 and tagLength !=0 and tagLength <=32:
                ssid = tags[:tagLength]
                
                #Made sure what is extracted is valid ASCII
                #Psycopg2 pukes otherwise
                try:
                    ssid = ssid.decode('ascii')
                except UnicodeDecodeError:
                    ssid = None
                    continue
                
                break 
                
            tags = tags[tagLength:]
            
        if ssid != None:
            
            #Query the database to find the ssid
            cur = conn.cursor()
            cur.execute('Select id from ssid where name = %s',(ssid,))
            r = cur.fetchone()
            if r == None:
                cur.execute('Insert into ssid (name) VALUES(%s) returning id;',(ssid,))
                r = cur.fetchone()
                ssuid, = r
                cur.close()    
                conn.commit()
            else:
                ssuid, = r
                cur.close()
                conn.rollback()
        else:
            ssuid = None
            
            
        #Query the database for a beacon/probe by the station
        #if it was observed in the past 5 minutes,
        #don't add this one to the database                
        cur = conn.cursor()
        
        update = False
        if isProbe:
            if ssuid is not None:
                cur.execute('Select seen from probe left join ssid on probe.ssid=ssid.id where station = %s and ssid.id = %s order by seen desc limit 1;', (suid,ssuid,))
            else:
                cur.execute('Select seen from probe where station = %s and ssid is null order by seen desc limit 1;', (suid,))
            r = cur.fetchone()
            
            if r == None:
                update = True
            else:
                seen, = r 
                if (datetime.datetime.utcnow() - seen).total_seconds() > (5*60):
                    update = True
                
            if update:
                cur.execute("""Insert into probe(station,ssid,seen) VALUES(%s,%s,current_timestamp at time zone 'utc')""",(suid,ssuid,))
                cur.close()
                conn.commit()
            else:
                cur.close()
                conn.rollback()

if __name__ == "__main__":
    iface = sys.argv[1]
    with open(sys.argv[2]) as fin:
        conf = json.load(fin)            
    oui = manuf.MacParser.refresh(manuf)
    handler = Handler(conf)                
    sniff(iface=iface,prn=handler,store=0)