import sqlite3

def seed_backroom_packs():
    # The exact 62 un-settled, un-returned packs currently sitting in your store
    raw_packs = [
        ('7566', '0184009'), ('7601', '0040934'), ('7611', '0157232'), ('7631', '0130633'), 
        ('7634', '0021861'), ('7634', '0022543'), ('7635', '0038662'), ('7636', '0047838'), 
        ('7639', '0110218'), ('7640', '0207935'), ('7648', '0027959'), ('7648', '0032244'), 
        ('7648', '0032247'), ('7651', '0021088'), ('7651', '0024156'), ('7656', '0014306'), 
        ('7658', '0023483'), ('7659', '0022980'), ('7660', '0020838'), ('7661', '0024473'), 
        ('7661', '0024516'), ('7530', '0083766'), ('7563', '0158221'), ('7566', '0177777'), 
        ('7575', '0361359'), ('7621', '0142961'), ('7638', '0129256'), ('7639', '0091668'), 
        ('7640', '0169309'), ('7641', '0027724'), ('7641', '0073350'), ('7644', '0036643'), 
        ('7649', '0037088'), ('7652', '0016473'), ('7653', '0016124'), ('7653', '0026595'), 
        ('7657', '0028912'), ('7575', '0343583'), ('7590', '0126742'), ('7624', '0070629'), 
        ('7640', '0137227'), ('7640', '0144942'), ('7641', '0051394'), ('7647', '0016435'), 
        ('7647', '0016912'), ('7648', '0015811'), ('7648', '0019289'), ('7649', '0018869'), 
        ('7649', '0018910'), ('7650', '0028634'), ('7650', '0028818'), ('7599', '0114358'), 
        ('7611', '0127375'), ('7631', '0101784'), ('7641', '0024406'), ('7641', '0045538'), 
        ('7644', '0022000'), ('7645', '0021686'), ('7645', '0021692'), ('7646', '0028739'), 
        ('7646', '0031055'), ('7647', '0013211')
    ]

    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    count = 0

    for game_num, pack_num in raw_packs:
        pack_id = f"{game_num}-{pack_num}"
        
        # Look up the game in the database to find how many tickets are in this specific pack
        game = conn.execute('SELECT tickets_per_pack FROM games WHERE game_number = ?', (game_num,)).fetchone()
        
        if game:
            starting_ticket = game['tickets_per_pack'] - 1
            
            # Insert or replace the pack into Backroom stock
            conn.execute('''
                INSERT OR REPLACE INTO packs (pack_id, game_number, status, slot_number, current_ticket) 
                VALUES (?, ?, 'BACKROOM', NULL, ?)
            ''', (pack_id, game_num, starting_ticket))
            count += 1
        else:
            print(f"Warning: Game {game_num} not found in database. Skipping pack {pack_id}.")

    conn.commit()
    conn.close()
    print(f"Success! {count} packs have been injected into your Backroom Inventory.")

if __name__ == '__main__':
    seed_backroom_packs()